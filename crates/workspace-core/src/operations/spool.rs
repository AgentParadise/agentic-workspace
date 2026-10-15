//! Bounded, append-only operation output with opaque generation cursors.
//!
//! Credential values registered with [`OperationSpool::with_redaction`]
//! never reach storage: each channel is scanned before its bytes are stored,
//! every occurrence becomes [`REDACTED`], and a channel's tail that could
//! still begin a value is held until the next append or
//! [`OperationSpool::finish`] decides it. The merged stream the channels
//! interleave into is scanned again before storing, since two channels'
//! fragments can meet there. Offsets, `end`, retention and gaps
//! all count stored (redacted) bytes, so cursors stay consistent whatever
//! the marker's length. The rule is the Python spool's, checked against
//! `tests/fixtures/output_redaction/cases.json`.

use std::collections::{BTreeMap, VecDeque};
use std::num::NonZeroU64;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;

use super::contract::{
    Base64, ErrorKind, NonEmpty, ObservationGap, ObservationPage, ObservationSchema,
    OperationError, OperationRef, OperationState,
};

static GENERATIONS: AtomicU64 = AtomicU64::new(0);

/// Suffixes a cursor once the drain holding it has crossed a retention gap,
/// so every later page carries `loss_detected`, as the Python spool does.
const LOSSY: &str = ":lossy";

/// What a registered value is replaced with.
pub const REDACTED: &[u8] = b"[REDACTED]";

/// Whether an inserted [`REDACTED`] could leave or rebuild `value`. An
/// occurrence that overlaps a marker either lies inside it or crosses one of
/// its ends, so it holds the marker's first or last byte. A value with
/// neither, and not inside the marker, can only come from workload bytes.
fn overlaps_marker(value: &[u8]) -> bool {
    let ends = [REDACTED[0], REDACTED[REDACTED.len() - 1]];
    REDACTED.windows(value.len()).any(|w| w == value) || value.iter().any(|b| ends.contains(b))
}

/// Replaces registered values in a byte stream, whatever its chunking: at
/// each position the longest value that matches is replaced, and a position
/// the stream has not yet decided is held until it is.
#[derive(Default)]
struct Redactor {
    /// Longest first, so the longest wins at a position. Never empty values.
    values: Vec<Vec<u8>>,
}

impl std::fmt::Debug for Redactor {
    // Never the values themselves.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Redactor({} values)", self.values.len())
    }
}

impl Redactor {
    /// Refuses a registration that replacement could leave in, or build
    /// into, stored or channel bytes. The error never names the value.
    fn new(values: impl IntoIterator<Item = Vec<u8>>) -> Result<Self, OperationError> {
        let mut values: Vec<Vec<u8>> = values.into_iter().filter(|v| !v.is_empty()).collect();
        if values.iter().any(|v| overlaps_marker(v)) {
            // Lowercase and never spelling the marker: a value refused for
            // lying inside the marker would otherwise be echoed here.
            return Err(OperationError::new(
                ErrorKind::RejectedPolicy,
                "a registered redaction value overlaps the redaction marker; \
                 replacement could leave or rebuild it, so it cannot be redacted",
                None,
            ));
        }
        values.sort_by(|a, b| b.len().cmp(&a.len()).then(a.cmp(b)));
        values.dedup();
        Ok(Self { values })
    }

    /// `(released, held)`: redacted bytes safe to store, and the raw tail
    /// still undecided. `last` decides everything, so nothing is held.
    fn scan(&self, data: &[u8], last: bool) -> (Vec<u8>, Vec<u8>) {
        let mut out = Vec::with_capacity(data.len());
        let mut at = 0;
        while at < data.len() {
            let tail = &data[at..];
            let mut decided = false;
            for value in &self.values {
                if tail.starts_with(value) {
                    out.extend_from_slice(REDACTED);
                    at += value.len();
                    decided = true;
                    break;
                }
                if !last && value.len() > tail.len() && value.starts_with(tail) {
                    return (out, tail.to_vec());
                }
            }
            if !decided {
                out.push(data[at]);
                at += 1;
            }
        }
        (out, Vec::new())
    }
}

/// Output retained up to `retention_bytes`, read in pages no larger than the
/// caller's bound once serialised. A cursor older than retention resumes at
/// the oldest retained byte and reports the evicted count as an exact gap.
pub struct OperationSpool {
    retention: usize,
    generation: String,
    retained: VecDeque<u8>,
    /// Absolute offset of the first retained byte.
    base: u64,
    redactor: Redactor,
    /// Per channel: raw bytes not yet stored because they may begin a value.
    held: BTreeMap<usize, Vec<u8>>,
    /// Channel-redacted bytes not yet stored because, merged with another
    /// channel's next bytes, they may begin a value.
    merged_held: Vec<u8>,
}

impl std::fmt::Debug for OperationSpool {
    // Lengths only: retained and held bytes can hold a value still undecided.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OperationSpool")
            .field("retention", &self.retention)
            .field("generation", &self.generation)
            .field("base", &self.base)
            .field("retained_len", &self.retained.len())
            .field("redactor", &self.redactor)
            .field(
                "held_lens",
                &self
                    .held
                    .iter()
                    .map(|(c, h)| (c, h.len()))
                    .collect::<Vec<_>>(),
            )
            .field("merged_held_len", &self.merged_held.len())
            .finish()
    }
}

impl OperationSpool {
    /// A spool that stores output exactly as appended.
    pub fn new(retention_bytes: NonZeroU64) -> Self {
        Self::assemble(retention_bytes, Redactor::default())
    }

    /// A spool that replaces every one of `values` with [`REDACTED`] before
    /// storing. No values is [`OperationSpool::new`]; empty values are ignored.
    /// A value the marker could leave or rebuild is `rejected_policy`.
    pub fn with_redaction(
        retention_bytes: NonZeroU64,
        values: Vec<Vec<u8>>,
    ) -> Result<Self, OperationError> {
        Ok(Self::assemble(retention_bytes, Redactor::new(values)?))
    }

    fn assemble(retention_bytes: NonZeroU64, redactor: Redactor) -> Self {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or_default();
        let sequence = GENERATIONS.fetch_add(1, Ordering::Relaxed);
        Self {
            retention: usize::try_from(retention_bytes.get()).unwrap_or(usize::MAX),
            generation: format!("{nanos:x}{:x}{sequence:x}", std::process::id()),
            retained: VecDeque::new(),
            base: 0,
            redactor,
            held: BTreeMap::new(),
            merged_held: Vec::new(),
        }
    }

    /// Absolute offset one past the last byte ever appended.
    pub fn end(&self) -> u64 {
        self.base + self.retained.len() as u64
    }

    /// Append single-channel output, evicting the oldest bytes beyond
    /// retention.
    pub fn append(&mut self, data: &[u8]) {
        self.append_channel(data, 0);
    }

    /// Append output read from `channel` and return that channel's redacted
    /// bytes, for a caller that keeps its own per-channel copy. Each channel
    /// is redacted on its own, then the merged stream is redacted again
    /// before storing, so neither copy ever holds a registered value.
    pub fn append_channel(&mut self, data: &[u8], channel: usize) -> Vec<u8> {
        if self.redactor.values.is_empty() {
            self.store(data);
            return data.to_vec();
        }
        let mut pending = self.held.remove(&channel).unwrap_or_default();
        pending.extend_from_slice(data);
        let (released, held) = self.redactor.scan(&pending, false);
        if !held.is_empty() {
            self.held.insert(channel, held);
        }
        self.merge(&released, false);
        released
    }

    /// Decide and store every held tail once the collector has read its
    /// last byte, returning what each channel released. Call before the
    /// terminal state is published, so `complete` covers the final bytes.
    pub fn finish(&mut self) -> Vec<(usize, Vec<u8>)> {
        let held = std::mem::take(&mut self.held);
        let released = held
            .into_iter()
            .map(|(channel, tail)| {
                let (released, _) = self.redactor.scan(&tail, true);
                self.merge(&released, false);
                (channel, released)
            })
            .collect();
        self.merge(&[], true);
        released
    }

    /// Store channel-redacted bytes once the merged stream decides them.
    fn merge(&mut self, data: &[u8], last: bool) {
        let mut pending = std::mem::take(&mut self.merged_held);
        pending.extend_from_slice(data);
        let (released, held) = self.redactor.scan(&pending, last);
        self.merged_held = held;
        self.store(&released);
    }

    fn store(&mut self, data: &[u8]) {
        self.retained.extend(data);
        let excess = self.retained.len().saturating_sub(self.retention);
        self.retained.drain(..excess);
        self.base += excess as u64;
    }

    /// The page after `cursor`, serialised to at most `max_page_bytes`. It is
    /// `complete` only when `state` is terminal and it is the empty page at
    /// the end: a page carrying the last bytes is not, so a reader drains the
    /// rest and only then sees `complete` (APSS behaviour fixture
    /// `late-read-drains-then-complete`).
    pub fn page(
        &self,
        operation: &OperationRef,
        state: OperationState,
        cursor: Option<&str>,
        max_page_bytes: usize,
    ) -> Result<ObservationPage, OperationError> {
        let (mut start, lossy) = self.resolve(operation, cursor)?;
        let gap = NonZeroU64::new(self.base.saturating_sub(start)).map(|lost| ObservationGap {
            lost_bytes: Some(lost),
        });
        let lossy = lossy || gap.is_some();
        start = start.max(self.base);
        let available = usize::try_from(self.end() - start).unwrap_or(usize::MAX);
        let fits = |size| {
            let page = self.build(operation, state, cursor, start, size, (gap, lossy));
            serde_json::to_vec(&page).is_ok_and(|bytes| bytes.len() <= max_page_bytes)
        };
        if !fits(0) {
            return Err(OperationError::new(
                ErrorKind::RejectedPolicy,
                format!(
                    "max_page_bytes={max_page_bytes} cannot hold an empty page envelope; \
                     request a larger page"
                ),
                Some(operation.clone()),
            ));
        }
        // The serialised page never shrinks as it carries more bytes, so the
        // largest size that fits is found by bisection. Taking the largest,
        // not the first a descent happens to land on, keeps the page size
        // monotonic in the bound whatever the identifiers' widths: a larger
        // bound never returns fewer bytes, and no reachable page is skipped.
        let (mut lo, mut hi) = (0, available.min(max_page_bytes / 4 * 3));
        while lo < hi {
            let mid = lo + (hi - lo).div_ceil(2);
            if fits(mid) { lo = mid } else { hi = mid - 1 }
        }
        Ok(self.build(operation, state, cursor, start, lo, (gap, lossy)))
    }

    fn build(
        &self,
        operation: &OperationRef,
        state: OperationState,
        cursor: Option<&str>,
        start: u64,
        size: usize,
        // The gap this page reports, and whether its drain has lost bytes.
        (gap, lossy): (Option<ObservationGap>, bool),
    ) -> ObservationPage {
        let lo = usize::try_from(start - self.base).unwrap_or(usize::MAX);
        let data: Vec<u8> = self.retained.range(lo..lo + size).copied().collect();
        let next = start + size as u64;
        ObservationPage {
            schema: Some(ObservationSchema::V1),
            operation: operation.clone(),
            state,
            cursor: self.cursor_or_origin(cursor),
            next_cursor: self.encode(next, lossy),
            data_b64: Base64::try_from(STANDARD.encode(&data)).expect("STANDARD is padded base64"),
            gap,
            complete: state.is_terminal()
                && size == 0
                && next == self.end()
                && self.held.is_empty()
                && self.merged_held.is_empty(),
            loss_detected: lossy.then_some(true),
        }
    }

    fn cursor_or_origin(&self, cursor: Option<&str>) -> NonEmpty {
        cursor
            .and_then(|c| NonEmpty::try_from(c).ok())
            .unwrap_or_else(|| self.encode(0, false))
    }

    fn encode(&self, offset: u64, lossy: bool) -> NonEmpty {
        let suffix = if lossy { LOSSY } else { "" };
        NonEmpty::try_from(format!("{}:{offset}{suffix}", self.generation)).expect("never empty")
    }

    fn resolve(
        &self,
        operation: &OperationRef,
        cursor: Option<&str>,
    ) -> Result<(u64, bool), OperationError> {
        let Some(cursor) = cursor else {
            return Ok((0, false));
        };
        let unknown = |why: &str| {
            OperationError::new(
                ErrorKind::UnknownOperation,
                format!(
                    "cursor {cursor:?} {why} operation {}",
                    operation.operation_id
                ),
                Some(operation.clone()),
            )
        };
        let (position, lossy) = match cursor.strip_suffix(LOSSY) {
            Some(position) => (position, true),
            None => (cursor, false),
        };
        let offset = position
            .rsplit_once(':')
            .filter(|(generation, _)| *generation == self.generation)
            .and_then(|(_, offset)| offset.parse::<u64>().ok())
            .ok_or_else(|| unknown("was not issued for"))?;
        if offset > self.end() {
            return Err(unknown("is beyond the end of"));
        }
        Ok((offset, lossy))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn operation() -> OperationRef {
        serde_json::from_value(serde_json::json!({
            "workspace": {"provider": "docker", "resource_id": "c1"},
            "operation_id": "op-1",
            "operation_key": "k1",
        }))
        .unwrap()
    }

    fn decoded(page: &ObservationPage) -> Vec<u8> {
        STANDARD.decode(page.data_b64.as_str()).unwrap()
    }

    #[test]
    fn every_page_fits_its_bound_and_pages_concatenate_to_the_output() {
        let mut spool = OperationSpool::new(NonZeroU64::new(1 << 20).unwrap());
        let output: Vec<u8> = (0..5000u32).map(|i| (i % 251) as u8).collect();
        spool.append(&output);
        let (mut cursor, mut seen) = (None::<String>, Vec::new());
        loop {
            let page = spool
                .page(
                    &operation(),
                    OperationState::Succeeded,
                    cursor.as_deref(),
                    512,
                )
                .unwrap();
            assert!(serde_json::to_vec(&page).unwrap().len() <= 512);
            assert!(page.gap.is_none());
            seen.extend(decoded(&page));
            cursor = Some(page.next_cursor.to_string());
            if page.complete {
                break;
            }
        }
        assert_eq!(seen, output);
    }

    #[test]
    fn the_last_bytes_are_not_complete_and_the_empty_page_after_them_is() {
        let mut spool = OperationSpool::new(NonZeroU64::new(1 << 20).unwrap());
        spool.append(b"hello world\n");
        let last = spool
            .page(&operation(), OperationState::Succeeded, None, 4096)
            .unwrap();
        assert_eq!(decoded(&last), b"hello world\n");
        assert!(!last.complete);
        let end = spool
            .page(
                &operation(),
                OperationState::Succeeded,
                Some(last.next_cursor.as_str()),
                4096,
            )
            .unwrap();
        assert_eq!(decoded(&end), b"");
        assert_eq!(end.next_cursor, last.next_cursor);
        assert!(end.complete);
        let running = spool
            .page(
                &operation(),
                OperationState::Running,
                Some(last.next_cursor.as_str()),
                4096,
            )
            .unwrap();
        assert!(!running.complete);
    }

    #[test]
    fn a_cursor_older_than_retention_reports_the_exact_gap() {
        let mut spool = OperationSpool::new(NonZeroU64::new(8).unwrap());
        spool.append(b"0123456789abcdef");
        let first = spool
            .page(&operation(), OperationState::Running, None, 4096)
            .unwrap();
        let origin = first.cursor.to_string();
        assert_eq!(first.gap.unwrap().lost_bytes, NonZeroU64::new(8));
        assert_eq!(decoded(&first), b"89abcdef");
        // The drain crossed a gap, so its cursor carries the loss on.
        let (generation, next) = first
            .next_cursor
            .as_str()
            .strip_suffix(LOSSY)
            .unwrap()
            .rsplit_once(':')
            .unwrap();
        assert_eq!(next, "16");
        assert_eq!(format!("{generation}:0"), origin);
    }

    /// The page after `cursor` under every bound up to 512, asserting a
    /// larger bound never returns fewer bytes, and the page the conformance
    /// driver takes: the smallest bound whose page carries `len` bytes or more.
    fn smallest_page_holding(
        spool: &OperationSpool,
        operation: &OperationRef,
        cursor: &str,
        len: usize,
    ) -> ObservationPage {
        let pages: Vec<ObservationPage> = (1..=512)
            .filter_map(|bound| {
                spool
                    .page(operation, OperationState::Running, Some(cursor), bound)
                    .ok()
            })
            .collect();
        for pair in pages.windows(2) {
            let (smaller, larger) = (decoded(&pair[0]).len(), decoded(&pair[1]).len());
            assert!(
                larger >= smaller,
                "{larger} bytes after {smaller} at a larger bound"
            );
        }
        pages
            .into_iter()
            .find(|page| decoded(page).len() >= len)
            .unwrap()
    }

    #[test]
    fn a_larger_bound_never_returns_fewer_bytes_at_any_identifier_width() {
        // Generations and operation keys vary in width with unrelated state
        // (process id, how many spools came before), so page sizing must not
        // depend on where those widths happen to fall.
        let output: Vec<u8> = (0..128u32).map(|i| b'a' + (i % 26) as u8).collect();
        for generation in 1..=16 {
            for key in 1..=6 {
                let at = format!("generation width {generation}, key width {key}");
                let mut spool = OperationSpool::new(NonZeroU64::new(64).unwrap());
                spool.generation = "f".repeat(generation);
                spool.append(&output);
                let operation: OperationRef = serde_json::from_value(serde_json::json!({
                    "workspace": {"provider": "docker", "resource_id": "c1"},
                    "operation_id": "op-1",
                    "operation_key": "k".repeat(key),
                }))
                .unwrap();
                // Both pages of the retention-gap fixture: six bytes and the
                // exact gap from the origin, then the bytes straight after.
                let origin = spool.encode(0, false).to_string();
                let first = smallest_page_holding(&spool, &operation, &origin, 6);
                assert_eq!(decoded(&first), output[64..70], "{at}");
                assert_eq!(first.gap.unwrap().lost_bytes, NonZeroU64::new(64), "{at}");
                let next = first.next_cursor.as_str();
                let second = smallest_page_holding(&spool, &operation, next, 6);
                assert_eq!(decoded(&second), output[70..76], "{at}");
                assert!(second.gap.is_none(), "{at}");
            }
        }
    }

    fn drain(spool: &OperationSpool, max: usize) -> Vec<u8> {
        let (mut cursor, mut seen) = (None::<String>, Vec::new());
        loop {
            let page = spool
                .page(
                    &operation(),
                    OperationState::Succeeded,
                    cursor.as_deref(),
                    max,
                )
                .unwrap();
            seen.extend(decoded(&page));
            cursor = Some(page.next_cursor.to_string());
            if page.complete {
                let end = cursor.unwrap().rsplit_once(':').unwrap().1.parse::<u64>();
                assert_eq!(end.unwrap(), spool.end());
                return seen;
            }
        }
    }

    const CASES: &str = include_str!("../../../../tests/fixtures/output_redaction/cases.json");

    #[test]
    fn every_split_of_every_fixture_stores_the_same_redacted_bytes() {
        let fixtures: serde_json::Value = serde_json::from_str(CASES).unwrap();
        assert_eq!(fixtures["marker"].as_str().unwrap().as_bytes(), REDACTED);
        for case in fixtures["cases"].as_array().unwrap() {
            let values: Vec<Vec<u8>> = case["values"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().as_bytes().to_vec())
                .collect();
            let output = case["output"].as_str().unwrap().as_bytes();
            let stored = case["stored"].as_str().unwrap().as_bytes();
            let mut splits = vec![vec![output.to_vec()]];
            for k in 0..=output.len() {
                splits.push(vec![output[..k].to_vec(), output[k..].to_vec()]);
            }
            splits.push(output.iter().map(|b| vec![*b]).collect());
            for chunks in splits {
                let mut spool = OperationSpool::with_redaction(
                    NonZeroU64::new(1 << 20).unwrap(),
                    values.clone(),
                )
                .unwrap();
                for chunk in &chunks {
                    spool.append(chunk);
                }
                spool.finish();
                let at = format!("{} {chunks:?}", case["name"]);
                assert_eq!(
                    spool.retained.iter().copied().collect::<Vec<_>>(),
                    stored,
                    "{at}"
                );
                assert_eq!(drain(&spool, 320), stored, "{at}");
                assert_eq!(spool.end(), stored.len() as u64, "{at}");
            }
        }
    }

    #[test]
    fn channels_merged_into_storage_never_form_a_value() {
        let fixtures: serde_json::Value = serde_json::from_str(CASES).unwrap();
        for case in fixtures["channel_cases"].as_array().unwrap() {
            let values: Vec<Vec<u8>> = case["values"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().as_bytes().to_vec())
                .collect();
            let whole: Vec<(usize, Vec<u8>)> = case["appends"]
                .as_array()
                .unwrap()
                .iter()
                .map(|a| {
                    let channel = usize::try_from(a[0].as_u64().unwrap()).unwrap();
                    (channel, a[1].as_str().unwrap().as_bytes().to_vec())
                })
                .collect();
            let stored = case["stored"].as_str().unwrap().as_bytes();
            // The appends as given, then with each one split at every offset.
            let mut splits = vec![whole.clone()];
            for (n, (channel, data)) in whole.iter().enumerate() {
                for k in 0..=data.len() {
                    let mut split = whole[..n].to_vec();
                    split.push((*channel, data[..k].to_vec()));
                    split.push((*channel, data[k..].to_vec()));
                    split.extend_from_slice(&whole[n + 1..]);
                    splits.push(split);
                }
            }
            for appends in splits {
                let mut spool = OperationSpool::with_redaction(
                    NonZeroU64::new(1 << 20).unwrap(),
                    values.clone(),
                )
                .unwrap();
                let mut copies: BTreeMap<usize, Vec<u8>> = BTreeMap::new();
                for (channel, chunk) in &appends {
                    let released = spool.append_channel(chunk, *channel);
                    copies.entry(*channel).or_default().extend(released);
                }
                for (channel, tail) in spool.finish() {
                    copies.entry(channel).or_default().extend(tail);
                }
                let at = format!("{} {appends:?}", case["name"]);
                assert_eq!(
                    spool.retained.iter().copied().collect::<Vec<_>>(),
                    stored,
                    "{at}"
                );
                assert_eq!(drain(&spool, 320), stored, "{at}");
                assert_eq!(spool.end(), stored.len() as u64, "{at}");
                for value in &values {
                    for copy in copies.values() {
                        assert!(!copy.windows(value.len()).any(|w| w == value), "{at}");
                    }
                }
            }
        }
    }

    const CANARY: &[u8] = b"canary-INVENTED-sk-7f3a";

    #[test]
    fn a_value_split_across_interleaved_channels_is_redacted() {
        let mut spool = OperationSpool::with_redaction(
            NonZeroU64::new(1 << 20).unwrap(),
            vec![CANARY.to_vec()],
        )
        .unwrap();
        spool.append(&[b"out:", &CANARY[..9]].concat());
        spool.append_channel(b"err line\n", 1);
        spool.append(&[&CANARY[9..], b"\n"].concat());
        spool.finish();
        assert_eq!(drain(&spool, 4096), b"out:err line\n[REDACTED]\n");
    }

    #[test]
    fn held_bytes_are_neither_counted_nor_complete_until_finished() {
        let mut spool = OperationSpool::with_redaction(
            NonZeroU64::new(1 << 20).unwrap(),
            vec![CANARY.to_vec()],
        )
        .unwrap();
        spool.append(&[b"ok ", &CANARY[..5]].concat());
        assert_eq!(spool.end(), 3);
        let first = spool
            .page(&operation(), OperationState::Succeeded, None, 4096)
            .unwrap();
        let after = spool
            .page(
                &operation(),
                OperationState::Succeeded,
                Some(first.next_cursor.as_str()),
                4096,
            )
            .unwrap();
        assert!(!after.complete);
        spool.finish();
        assert_eq!(drain(&spool, 4096), [b"ok ", &CANARY[..5]].concat());
        assert!(!format!("{spool:?}").contains("canary"));
    }

    #[test]
    fn debug_never_shows_held_or_stored_bytes_in_any_encoding() {
        // The shorter value is complete but held: a longer one may follow.
        let short = b"canary-INVENTED-short";
        let mut spool = OperationSpool::with_redaction(
            NonZeroU64::new(1000).unwrap(),
            vec![short.to_vec(), [short.as_slice(), b"-longer"].concat()],
        )
        .unwrap();
        spool.append(b"stored ");
        spool.append_channel(short, 1);
        assert_eq!(spool.end(), 7);
        let debug = format!("{spool:?}");
        for shown in [format!("{short:?}"), format!("{:?}", b"stored ")] {
            let numbers = shown.trim_matches(['[', ']']);
            assert!(!debug.contains(numbers), "{debug}");
        }
        assert!(!debug.contains("canary"), "{debug}");
    }

    #[test]
    fn a_registration_the_marker_could_leave_or_rebuild_is_refused() {
        let fixtures: serde_json::Value = serde_json::from_str(CASES).unwrap();
        for case in fixtures["refused_registrations"].as_array().unwrap() {
            let values: Vec<&str> = case["values"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap())
                .collect();
            let error = OperationSpool::with_redaction(
                NonZeroU64::new(64).unwrap(),
                values.iter().map(|v| v.as_bytes().to_vec()).collect(),
            )
            .unwrap_err();
            assert_eq!(error.kind, ErrorKind::RejectedPolicy, "{}", case["name"]);
            let serialized = serde_json::to_string(&error).unwrap();
            for value in values {
                assert!(!error.message.as_str().contains(value), "{}", case["name"]);
                assert!(!serialized.contains(value), "{}", case["name"]);
            }
        }
    }

    #[test]
    fn retention_and_gaps_count_stored_bytes() {
        let mut spool =
            OperationSpool::with_redaction(NonZeroU64::new(12).unwrap(), vec![CANARY.to_vec()])
                .unwrap();
        spool.append(&[CANARY, b" tail-bytes"].concat());
        spool.finish();
        let stored = b"[REDACTED] tail-bytes";
        assert_eq!(spool.end(), stored.len() as u64);
        let page = spool
            .page(&operation(), OperationState::Succeeded, None, 4096)
            .unwrap();
        assert_eq!(
            page.gap.unwrap().lost_bytes,
            NonZeroU64::new(stored.len() as u64 - 12)
        );
        assert_eq!(decoded(&page), stored[stored.len() - 12..]);
    }

    #[test]
    fn loss_is_sticky_from_a_redacted_gap_to_the_final_empty_page() {
        let mut spool = OperationSpool::with_redaction(
            NonZeroU64::new(12).unwrap(),
            vec![b"invented-token".to_vec()],
        )
        .unwrap();
        spool.append(b"invented-token tail-bytes");
        spool.finish();
        // Pages a few bytes larger than the envelope, so the drain crosses
        // the gap and then reads on.
        let whole = spool
            .page(&operation(), OperationState::Succeeded, None, 4096)
            .unwrap();
        let envelope = serde_json::to_vec(&whole).unwrap().len() - whole.data_b64.as_str().len();
        let bound = envelope + LOSSY.len() + 8;
        let mut page = spool
            .page(&operation(), OperationState::Succeeded, None, bound)
            .unwrap();
        assert!(page.gap.is_some());
        let mut pages = 1;
        loop {
            assert_eq!(page.loss_detected, Some(true), "page {pages}");
            if page.complete {
                break;
            }
            page = spool
                .page(
                    &operation(),
                    OperationState::Succeeded,
                    Some(page.next_cursor.as_str()),
                    bound,
                )
                .unwrap();
            assert!(page.gap.is_none(), "page {pages}");
            pages += 1;
        }
        assert!(pages >= 3, "{pages}");
        assert!(decoded(&page).is_empty());
        // A drain that never crossed a gap reports no loss.
        let fresh = spool.encode(spool.base, false).to_string();
        let page = spool
            .page(&operation(), OperationState::Succeeded, Some(&fresh), 4096)
            .unwrap();
        assert_eq!(page.loss_detected, None);
    }

    #[test]
    fn pages_drained_before_any_eviction_carry_no_loss() {
        let mut spool = OperationSpool::with_redaction(
            NonZeroU64::new(64).unwrap(),
            vec![b"invented-token".to_vec()],
        )
        .unwrap();
        spool.append(b"invented-token tail-bytes and more");
        spool.finish();
        let whole = spool
            .page(&operation(), OperationState::Succeeded, None, 4096)
            .unwrap();
        let envelope = serde_json::to_vec(&whole).unwrap().len() - whole.data_b64.as_str().len();
        let bound = envelope + 8;
        let (mut cursor, mut seen, mut pages) = (None, Vec::new(), 0);
        loop {
            let page = spool
                .page(
                    &operation(),
                    OperationState::Succeeded,
                    cursor.as_deref(),
                    bound,
                )
                .unwrap();
            pages += 1;
            let wire: serde_json::Value = serde_json::to_value(&page).unwrap();
            assert_eq!(wire.get("gap"), None, "page {pages}");
            assert_eq!(wire.get("loss_detected"), None, "page {pages}");
            seen.extend(decoded(&page));
            cursor = Some(page.next_cursor.to_string());
            if page.complete {
                break;
            }
        }
        assert!(pages >= 3, "{pages}");
        assert_eq!(seen, b"[REDACTED] tail-bytes and more");
    }

    #[test]
    fn a_foreign_cursor_is_unknown_operation() {
        let spool = OperationSpool::new(NonZeroU64::new(8).unwrap());
        let other = OperationSpool::new(NonZeroU64::new(8).unwrap());
        let foreign = other.encode(0, false).to_string();
        let error = spool
            .page(&operation(), OperationState::Running, Some(&foreign), 4096)
            .unwrap_err();
        assert_eq!(error.kind, ErrorKind::UnknownOperation);
    }

    #[test]
    fn a_page_bound_too_small_for_the_envelope_is_rejected_policy() {
        let spool = OperationSpool::new(NonZeroU64::new(8).unwrap());
        let error = spool
            .page(&operation(), OperationState::Running, None, 16)
            .unwrap_err();
        assert_eq!(error.kind, ErrorKind::RejectedPolicy);
    }
}
