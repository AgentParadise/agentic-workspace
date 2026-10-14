//! Bounded, append-only operation output with opaque generation cursors.

use std::collections::VecDeque;
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

/// Output retained up to `retention_bytes`, read in pages no larger than the
/// caller's bound once serialised. A cursor older than retention resumes at
/// the oldest retained byte and reports the evicted count as an exact gap.
#[derive(Debug)]
pub struct OperationSpool {
    retention: usize,
    generation: String,
    retained: VecDeque<u8>,
    /// Absolute offset of the first retained byte.
    base: u64,
}

impl OperationSpool {
    pub fn new(retention_bytes: NonZeroU64) -> Self {
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
        }
    }

    /// Absolute offset one past the last byte ever appended.
    pub fn end(&self) -> u64 {
        self.base + self.retained.len() as u64
    }

    /// Append output, evicting the oldest bytes beyond retention.
    pub fn append(&mut self, data: &[u8]) {
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
        let mut start = self.resolve(operation, cursor)?;
        let gap = NonZeroU64::new(self.base.saturating_sub(start)).map(|lost| ObservationGap {
            lost_bytes: Some(lost),
        });
        start = start.max(self.base);
        let available = usize::try_from(self.end() - start).unwrap_or(usize::MAX);
        let fits = |size| {
            let page = self.build(operation, state, cursor, start, size, gap);
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
        Ok(self.build(operation, state, cursor, start, lo, gap))
    }

    fn build(
        &self,
        operation: &OperationRef,
        state: OperationState,
        cursor: Option<&str>,
        start: u64,
        size: usize,
        gap: Option<ObservationGap>,
    ) -> ObservationPage {
        let lo = usize::try_from(start - self.base).unwrap_or(usize::MAX);
        let data: Vec<u8> = self.retained.range(lo..lo + size).copied().collect();
        let next = start + size as u64;
        ObservationPage {
            schema: Some(ObservationSchema::V1),
            operation: operation.clone(),
            state,
            cursor: self.cursor_or_origin(cursor),
            next_cursor: self.encode(next),
            data_b64: Base64::try_from(STANDARD.encode(&data)).expect("STANDARD is padded base64"),
            gap,
            complete: state.is_terminal() && size == 0 && next == self.end(),
        }
    }

    fn cursor_or_origin(&self, cursor: Option<&str>) -> NonEmpty {
        cursor
            .and_then(|c| NonEmpty::try_from(c).ok())
            .unwrap_or_else(|| self.encode(0))
    }

    fn encode(&self, offset: u64) -> NonEmpty {
        NonEmpty::try_from(format!("{}:{offset}", self.generation)).expect("never empty")
    }

    fn resolve(
        &self,
        operation: &OperationRef,
        cursor: Option<&str>,
    ) -> Result<u64, OperationError> {
        let Some(cursor) = cursor else { return Ok(0) };
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
        let offset = cursor
            .rsplit_once(':')
            .filter(|(generation, _)| *generation == self.generation)
            .and_then(|(_, offset)| offset.parse::<u64>().ok())
            .ok_or_else(|| unknown("was not issued for"))?;
        if offset > self.end() {
            return Err(unknown("is beyond the end of"));
        }
        Ok(offset)
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
        let (generation, next) = first.next_cursor.as_str().rsplit_once(':').unwrap();
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
                let origin = spool.encode(0).to_string();
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

    #[test]
    fn a_foreign_cursor_is_unknown_operation() {
        let spool = OperationSpool::new(NonZeroU64::new(8).unwrap());
        let other = OperationSpool::new(NonZeroU64::new(8).unwrap());
        let foreign = other.encode(0).to_string();
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
