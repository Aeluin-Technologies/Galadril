//! Pin every GET to an immutable object and verify exact HTTP ranges.

use std::io;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::Ordering::Relaxed;
use std::time::Duration;

use aws_sdk_s3::Client;
use aws_sdk_s3::error::SdkError;
use aws_sdk_s3::primitives::ByteStream;
#[cfg(feature = "parser")]
use sha2::{Digest, Sha256};

use super::cache::Key;
use super::publication::artifact_key;
use super::{Artifact, Generation, RemoteError, RemoteStore, Window, within};
use crate::archive::container::{MAX_BLOCK, digest};
use crate::archive::{RangeReader, RangeRequest};

#[derive(Clone)]
pub(super) struct S3 {
    client: Client,
    bucket: Arc<str>,
    prefix: Arc<str>,
    timeout: Duration,
}

pub(super) enum Condition<'a> {
    Create,
    Matches(&'a str),
}

impl S3 {
    pub fn new(
        client: Client,
        bucket: &str,
        prefix: String,
        timeout: Duration,
    ) -> Self {
        Self {
            client,
            bucket: bucket.into(),
            prefix: prefix.into(),
            timeout,
        }
    }

    pub async fn ensure_unversioned(&self) -> Result<(), RemoteError> {
        let response = self
            .timed(async {
                self.client
                    .get_bucket_versioning()
                    .bucket(self.bucket.as_ref())
                    .send()
                    .await
                    .map_err(sdk)
            })
            .await?;
        if response.status().is_some() {
            return Err(RemoteError::UnsupportedStorage);
        }
        Ok(())
    }

    fn timed<'a, T>(
        &self,
        future: impl Future<Output = Result<T, RemoteError>> + Send + 'a,
    ) -> futures::future::BoxFuture<'a, Result<T, RemoteError>> {
        let timeout = self.timeout;
        // Smithy request futures are large. Boxing the transport once keeps
        // concurrent activation and verification within normal thread stacks.
        Box::pin(async move {
            tokio::time::timeout(timeout, future)
                .await
                .map_err(|_| RemoteError::Timeout)?
        })
    }

    fn key(&self, suffix: &str) -> String {
        format!("{}{suffix}", self.prefix)
    }

    pub async fn probe(&self) -> Result<(), RemoteError> {
        let key = format!("probe/{}", Generation::new());
        let result = self.probe_key(&key).await;
        let cleanup = self.delete(&key).await;
        match (result, cleanup) {
            (Ok(()), Ok(())) => Ok(()),
            (Err(error), _) | (_, Err(error)) => {
                tracing::debug!(event.name = "gol.remote.storage_probe_failed", reason = ?error);
                Err(RemoteError::UnsupportedStorage)
            },
        }
    }

    async fn probe_key(&self, key: &str) -> Result<(), RemoteError> {
        let etag = self
            .put(key, b"initial".to_vec(), Condition::Create)
            .await?;
        let create = self
            .put(key, b"duplicate".to_vec(), Condition::Create)
            .await;
        let stale = self
            .put(
                key,
                b"stale".to_vec(),
                Condition::Matches("\"00000000000000000000000000000000\""),
            )
            .await;
        if !matches!(
            (create, stale),
            (Err(RemoteError::Conflict), Err(RemoteError::Conflict))
        ) {
            return Err(RemoteError::UnsupportedStorage);
        }
        let (first, second) = futures::join!(
            self.put(key, b"first".to_vec(), Condition::Matches(&etag)),
            self.put(key, b"second".to_vec(), Condition::Matches(&etag)),
        );
        let (content, updated) = match (first, second) {
            (Ok(updated), Err(RemoteError::Conflict)) => {
                (b"first".as_slice(), updated)
            },
            (Err(RemoteError::Conflict), Ok(updated)) => {
                (b"second".as_slice(), updated)
            },
            _ => return Err(RemoteError::UnsupportedStorage),
        };
        let stored = self.get(key, 64).await?.ok_or(RemoteError::Missing)?;
        if updated == etag || stored.0 != content || stored.1 != updated {
            return Err(RemoteError::UnsupportedStorage);
        }
        Ok(())
    }

    pub async fn put(
        &self,
        key: &str,
        payload: Vec<u8>,
        condition: Condition<'_>,
    ) -> Result<Box<str>, RemoteError> {
        let request = self
            .client
            .put_object()
            .bucket(self.bucket.as_ref())
            .key(self.key(key))
            .body(ByteStream::from(payload));
        let request = match condition {
            Condition::Create => request.if_none_match("*"),
            Condition::Matches(etag) => request.if_match(etag),
        };
        self.timed(async {
            let result = request.send().await.map_err(sdk)?;
            unversioned(result.version_id())?;
            etag(result.e_tag())
        })
        .await
    }

    pub async fn get(
        &self,
        key: &str,
        limit: usize,
    ) -> Result<Option<(Vec<u8>, Box<str>)>, RemoteError> {
        self.timed(async {
            let mut response = match self
                .client
                .get_object()
                .bucket(self.bucket.as_ref())
                .key(self.key(key))
                .send()
                .await
                .map_err(sdk)
            {
                Ok(response) => response,
                Err(RemoteError::Missing) => return Ok(None),
                Err(error) => return Err(error),
            };
            unversioned(response.version_id())?;
            let etag = etag(response.e_tag())?;
            let length = length(response.content_length())?;
            if length > limit || response.content_range().is_some() {
                return Err(RemoteError::Integrity);
            }
            Ok(Some((read_exact(&mut response.body, length).await?, etag)))
        })
        .await
    }

    #[cfg(feature = "parser")]
    pub async fn verify(
        &self,
        key: &str,
        expected: &[u8],
        etag: &str,
    ) -> Result<(), RemoteError> {
        let expected_digest = digest(expected);
        self.timed(async {
            let mut response = self
                .client
                .get_object()
                .bucket(self.bucket.as_ref())
                .key(self.key(key))
                .if_match(etag)
                .send()
                .await
                .map_err(sdk)?;
            unversioned(response.version_id())?;
            if response.e_tag() != Some(etag) ||
                length(response.content_length())? != expected.len() ||
                response.content_range().is_some()
            {
                return Err(RemoteError::Integrity);
            }
            let mut hash = Sha256::new();
            let mut received = 0usize;
            while let Some(chunk) = response
                .body
                .try_next()
                .await
                .map_err(|_| RemoteError::Integrity)?
            {
                received = received
                    .checked_add(chunk.len())
                    .ok_or(RemoteError::Integrity)?;
                if received > expected.len() {
                    return Err(RemoteError::Integrity);
                }
                hash.update(&chunk);
            }
            let actual: [u8; 32] = hash.finalize().into();
            if received != expected.len() || actual != expected_digest {
                return Err(RemoteError::Integrity);
            }
            Ok(())
        })
        .await
    }

    async fn range(
        &self,
        key: &str,
        artifact: &Artifact,
        offset: u64,
        count: usize,
    ) -> Result<Vec<u8>, RemoteError> {
        let last = end(offset, count, artifact.bytes)?
            .checked_sub(1)
            .ok_or(RemoteError::Integrity)?;
        self.timed(async {
            let mut response = self
                .client
                .get_object()
                .bucket(self.bucket.as_ref())
                .key(self.key(key))
                .range(format!("bytes={offset}-{last}"))
                .if_match(artifact.etag.as_ref())
                .send()
                .await
                .map_err(sdk)?;
            unversioned(response.version_id())?;
            let expected = format!("bytes {offset}-{last}/{}", artifact.bytes);
            if response.e_tag() != Some(artifact.etag.as_ref()) ||
                response.content_range() != Some(expected.as_str()) ||
                length(response.content_length())? != count
            {
                return Err(RemoteError::Integrity);
            }
            read_exact(&mut response.body, count).await
        })
        .await
    }

    pub async fn delete(&self, key: &str) -> Result<(), RemoteError> {
        self.timed(async {
            let output = self
                .client
                .delete_object()
                .bucket(self.bucket.as_ref())
                .key(self.key(key))
                .send()
                .await
                .map_err(sdk)?;
            unversioned(output.version_id())
        })
        .await
    }

    pub async fn list_artifacts(
        &self,
    ) -> Result<Vec<(String, Generation)>, RemoteError> {
        let prefix = self.key("artifacts/");
        let mut continuation = None;
        let mut artifacts = Vec::new();
        loop {
            let page = self
                .timed(async {
                    self.client
                        .list_objects_v2()
                        .bucket(self.bucket.as_ref())
                        .prefix(&prefix)
                        .set_continuation_token(continuation.clone())
                        .send()
                        .await
                        .map_err(sdk)
                })
                .await?;
            for entry in page.contents() {
                if let Some(full_key) = entry.key() &&
                    let Some(name) = full_key.strip_prefix(&prefix) &&
                    let Some(uuid) = name.strip_suffix(".golc") &&
                    let Ok(id) = uuid::Uuid::parse_str(uuid) &&
                    id.simple().to_string() == uuid
                {
                    artifacts
                        .push((format!("artifacts/{name}"), Generation(id)));
                }
            }
            if !page.is_truncated().unwrap_or(false) {
                return Ok(artifacts);
            }
            let next = page
                .next_continuation_token()
                .filter(|next| {
                    Some(*next) != continuation.as_deref() && !next.is_empty()
                })
                .ok_or(RemoteError::Integrity)?;
            continuation = Some(next.to_owned());
        }
    }
}

fn sdk<E>(error: SdkError<E>) -> RemoteError {
    match error
        .raw_response()
        .map(|response| response.status().as_u16())
    {
        Some(404) => RemoteError::Missing,
        Some(409 | 412) => RemoteError::Conflict,
        Some(408 | 504) => RemoteError::Timeout,
        Some(416) => RemoteError::Integrity,
        _ if matches!(error, SdkError::TimeoutError(_)) => {
            RemoteError::Timeout
        },
        _ => RemoteError::Unavailable,
    }
}

fn unversioned(version: Option<&str>) -> Result<(), RemoteError> {
    if version.is_some_and(|version| version != "null") {
        return Err(RemoteError::UnsupportedStorage);
    }
    Ok(())
}

fn etag(tag: Option<&str>) -> Result<Box<str>, RemoteError> {
    tag.filter(|tag| !tag.is_empty())
        .map(Into::into)
        .ok_or(RemoteError::Integrity)
}

fn length(length: Option<i64>) -> Result<usize, RemoteError> {
    length
        .and_then(|length| length.try_into().ok())
        .ok_or(RemoteError::Integrity)
}

fn end(offset: u64, count: usize, length: u64) -> Result<u64, RemoteError> {
    if !(1..=MAX_BLOCK).contains(&count) {
        return Err(RemoteError::Integrity);
    }
    offset
        .checked_add(count as u64)
        .filter(|end| *end <= length)
        .ok_or(RemoteError::Integrity)
}

async fn read_exact(
    body: &mut ByteStream,
    expected: usize,
) -> Result<Vec<u8>, RemoteError> {
    let mut output = Vec::with_capacity(expected);
    while let Some(chunk) =
        body.try_next().await.map_err(|_| RemoteError::Integrity)?
    {
        if chunk.len() > expected - output.len() {
            return Err(RemoteError::Integrity);
        }
        output.extend_from_slice(&chunk);
    }
    if output.len() != expected {
        return Err(RemoteError::Integrity);
    }
    Ok(output)
}

pub(super) struct Source {
    store: RemoteStore,
    artifact: Artifact,
    key: String,
    lifetime: Window,
}

impl Source {
    pub fn new(
        store: &RemoteStore,
        artifact: Artifact,
        lifetime: Window,
    ) -> Self {
        let key = artifact_key(artifact.id);
        Self {
            store: store.clone(),
            artifact,
            key,
            lifetime,
        }
    }

    async fn get(
        &self,
        range: RangeRequest,
    ) -> Result<Arc<[u8]>, RemoteError> {
        end(range.offset, range.length, self.artifact.bytes)?;
        let key = Key::new(&self.artifact, range);
        if let Some(bytes) =
            self.store.cache.get(key, &self.store.counters).await
        {
            return Ok(bytes);
        }
        let bytes = self.download(range.offset, range.length).await?;
        self.remember(range, &bytes).await
    }

    async fn remember(
        &self,
        range: RangeRequest,
        bytes: &[u8],
    ) -> Result<Arc<[u8]>, RemoteError> {
        if bytes.len() != range.length || digest(bytes) != range.sha256 {
            return Err(RemoteError::Integrity);
        }
        let authenticated: Arc<[u8]> = bytes.into();
        self.store
            .cache
            .put(Key::new(&self.artifact, range), &authenticated)
            .await;
        Ok(authenticated)
    }

    async fn download(
        &self,
        offset: u64,
        count: usize,
    ) -> Result<Vec<u8>, RemoteError> {
        self.store.counters.requests.fetch_add(1, Relaxed);
        let result = self
            .store
            .storage
            .range(&self.key, &self.artifact, offset, count)
            .await;
        match &result {
            Ok(bytes) => {
                self.store
                    .counters
                    .bytes
                    .fetch_add(bytes.len() as u64, Relaxed);
                tracing::trace!(
                    event.name = "gol.remote.range",
                    byte_count = bytes.len()
                );
            },
            Err(error) => {
                tracing::debug!(event.name = "gol.remote.range_failed", reason = ?error)
            },
        }
        result
    }

    async fn batch(
        &self,
        ranges: &[RangeRequest],
    ) -> Result<Vec<Arc<[u8]>>, RemoteError> {
        let mut results = vec![None; ranges.len()];
        let mut misses = Vec::new();
        for (index, range) in ranges.iter().enumerate() {
            let end = end(range.offset, range.length, self.artifact.bytes)?;
            let key = Key::new(&self.artifact, *range);
            let slot = results.get_mut(index).ok_or(RemoteError::Integrity)?;
            *slot = self.store.cache.get(key, &self.store.counters).await;
            if slot.is_none() {
                misses.push((index, *range, end));
            }
        }
        let groups = group_ranges(misses);
        let downloads = futures::future::try_join_all(groups.into_iter().map(
            |group| async move {
                let bytes = self
                    .download(group.start, (group.end - group.start) as usize)
                    .await?;
                Ok::<_, RemoteError>((group, bytes))
            },
        ))
        .await?;
        for (group, bytes) in downloads {
            for (index, range) in group.members {
                let start = (range.offset - group.start) as usize;
                let slice = bytes
                    .get(start..start + range.length)
                    .ok_or(RemoteError::Integrity)?;
                let slot =
                    results.get_mut(index).ok_or(RemoteError::Integrity)?;
                *slot = Some(self.remember(range, slice).await?);
            }
        }
        results
            .into_iter()
            .map(|result| result.ok_or(RemoteError::Integrity))
            .collect()
    }
}

struct Group {
    start: u64,
    end: u64,
    members: Vec<(usize, RangeRequest)>,
}

fn group_ranges(mut misses: Vec<(usize, RangeRequest, u64)>) -> Vec<Group> {
    misses.sort_unstable_by_key(|(_, range, _)| range.offset);
    let mut groups: Vec<Group> = Vec::new();
    for (index, range, end) in misses {
        // A gap may contain a protected projection or unrelated record.
        if let Some(last) = groups.last_mut() &&
            last.end == range.offset &&
            end - last.start <= MAX_BLOCK as u64
        {
            last.end = end;
            last.members.push((index, range));
        } else {
            groups.push(Group {
                start: range.offset,
                end,
                members: vec![(index, range)],
            });
        }
    }
    groups
}

impl RangeReader for Source {
    fn length(&self) -> io::Result<u64> {
        Ok(self.artifact.bytes)
    }

    fn read_exact_at(
        &self,
        _offset: u64,
        _buffer: &mut [u8],
    ) -> io::Result<()> {
        Err(io::ErrorKind::Unsupported.into())
    }

    fn fetch(
        &self,
        range: RangeRequest,
    ) -> impl Future<Output = io::Result<Arc<[u8]>>> + Send {
        // Keep the SDK's transport state out of every archive query future.
        let request: Pin<Box<dyn Future<Output = _> + Send>> =
            Box::pin(async move {
                within(self.lifetime, self.get(range))
                    .await
                    .map_err(RemoteError::io)
            });
        request
    }

    fn fetch_many<'a>(
        &'a self,
        ranges: &'a [RangeRequest],
    ) -> impl Future<Output = io::Result<Vec<Arc<[u8]>>>> + Send + 'a {
        let request: Pin<Box<dyn Future<Output = _> + Send>> =
            Box::pin(async move {
                within(self.lifetime, self.batch(ranges))
                    .await
                    .map_err(RemoteError::io)
            });
        request
    }

    fn cache_index(&self) -> bool {
        false
    }

    fn parallelism(&self) -> usize {
        self.store.options.parallelism
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use aws_sdk_s3::config::retry::RetryConfig;
    use aws_sdk_s3::config::{BehaviorVersion, Credentials, Region};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    use super::{Artifact, Generation, RemoteError, S3, group_ranges};
    use crate::archive::RangeRequest;

    type TestError = Box<dyn std::error::Error>;

    #[test]
    fn batches_only_adjacent_bounded_ranges() {
        let range = |index, offset, length| {
            (
                index,
                RangeRequest {
                    offset,
                    length,
                    sha256: [0; 32],
                },
                offset + length as u64,
            )
        };
        let groups = group_ranges(vec![
            range(0, 20, 4),
            range(1, 10, 8),
            range(2, 24, 4),
            range(3, 28, super::MAX_BLOCK),
        ]);
        let windows: Vec<_> = groups
            .iter()
            .map(|group| (group.start, group.end, group.members.len()))
            .collect();
        assert_eq!(
            windows,
            [
                (10, 18, 1),
                (20, 28, 2),
                (28, 28 + super::MAX_BLOCK as u64, 1)
            ]
        );
    }

    async fn faulty_response(
        headers: &'static str,
        body: &'static [u8],
        delay: Duration,
    ) -> Result<RemoteError, TestError> {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let endpoint = format!("http://{}", listener.local_addr()?);
        let peer = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await?;
            let mut request = Vec::new();
            loop {
                let mut chunk = [0; 256];
                let count = socket.read(&mut chunk).await?;
                let data = chunk
                    .get(..count)
                    .ok_or(std::io::ErrorKind::InvalidData)?;
                request.extend_from_slice(data);
                if count == 0 || request.len() > 8192 {
                    return Err(std::io::ErrorKind::InvalidData.into());
                }
                if request.windows(4).any(|marker| marker == b"\r\n\r\n") {
                    break;
                }
            }
            let request =
                String::from_utf8_lossy(&request).to_ascii_lowercase();
            if !request.contains("range: bytes=0-63") ||
                !request.contains("if-match: \"fixture\"")
            {
                return Err(std::io::ErrorKind::InvalidInput.into());
            }
            socket.write_all(headers.as_bytes()).await?;
            if !delay.is_zero() {
                tokio::time::sleep(delay).await;
            }
            if !body.is_empty() {
                socket.write_all(body).await?;
            }
            socket.shutdown().await
        });
        let client = aws_sdk_s3::Client::from_conf(
            aws_sdk_s3::config::Builder::new()
                .behavior_version(BehaviorVersion::latest())
                .region(Region::new("us-east-1"))
                .credentials_provider(Credentials::new(
                    "fixture_user",
                    "fixture_secret",
                    None,
                    None,
                    "test",
                ))
                .retry_config(RetryConfig::disabled())
                .endpoint_url(endpoint)
                .force_path_style(true)
                .build(),
        );
        let storage = S3::new(
            client,
            "fixture",
            "fixture/".into(),
            Duration::from_millis(350),
        );
        let artifact = Artifact {
            id: Generation::new(),
            pin: [0; 32],
            bytes: 100,
            etag: "\"fixture\"".into(),
        };
        let response = storage.range("object", &artifact, 0, 64).await;
        if delay.is_zero() {
            peer.await??;
        } else {
            peer.abort();
        }
        response
            .err()
            .ok_or_else(|| "faulty GET was accepted".into())
    }

    #[tokio::test]
    #[ignore = "opens an ephemeral loopback socket for HTTP fault injection"]
    async fn http_incomplete_or_non_range_responses_are_rejected()
    -> Result<(), TestError> {
        let partial = "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes 0-63/100\r\nContent-Length: 64\r\nETag: \"fixture\"\r\nConnection: close\r\n\r\n";
        let whole = "HTTP/1.1 200 OK\r\nContent-Length: 100\r\nETag: \"fixture\"\r\nConnection: close\r\n\r\n";
        assert_eq!(
            faulty_response(partial, b"short", Duration::ZERO).await?,
            RemoteError::Integrity
        );
        assert_eq!(
            faulty_response(whole, &[0; 100], Duration::ZERO).await?,
            RemoteError::Integrity
        );
        assert_eq!(
            faulty_response(partial, b"", Duration::from_secs(2)).await?,
            RemoteError::Timeout
        );
        Ok(())
    }
}
