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
    /// `complete` only when `state` is terminal and nothing remains to read.
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
        let mut size = available.min(max_page_bytes / 4 * 3);
        loop {
            let page = self.build(operation, state, cursor, start, size, gap);
            let fits = serde_json::to_vec(&page).map(|bytes| bytes.len() <= max_page_bytes);
            if fits.unwrap_or(false) {
                return Ok(page);
            }
            if size == 0 {
                return Err(OperationError::new(
                    ErrorKind::RejectedPolicy,
                    format!(
                        "max_page_bytes={max_page_bytes} cannot hold an empty page envelope; \
                         request a larger page"
                    ),
                    Some(operation.clone()),
                ));
            }
            size = size.saturating_sub((size / 8).max(3));
        }
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
            complete: state.is_terminal() && next == self.end(),
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
