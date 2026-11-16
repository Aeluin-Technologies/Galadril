//! Test remote disclosure and publication against real RustFS and SpiceDB.

#[path = "rustfs.rs"]
pub(super) mod rustfs;

use std::time::{Duration, Instant};

use galadril_ontology::Primitive;
use galadril_ontology::archive::remote::{
    DiskCache, Generation, ReadMetrics, RemoteError, RemoteOptions,
    RemoteStore,
};
use galadril_ontology::archive::{ArchiveReader, Direction, compile_archive};
use galadril_ontology::authorization::{
    Consistency, Operation, authorize, compile,
};

use super::{
    Runtime, TestResult, access, dependencies, environment, fixture, object,
    project, runtime, seed, start_spice, tuple, update,
};

const TENANT: &str = "tenant_a";
const STREAM: &str = "remote.fixture";

fn artifact_key(generation: Generation) -> String {
    format!("gol/v1/{TENANT}/{STREAM}/artifacts/{generation}.golc")
}

async fn connect(
    storage: &rustfs::RustFs,
    options: RemoteOptions,
) -> Result<RemoteStore, rustfs::TestError> {
    Ok(RemoteStore::connect(
        storage.client.clone(),
        rustfs::BUCKET,
        TENANT.parse()?,
        STREAM.parse()?,
        options,
    )
    .await?)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "starts pinned RustFS and SpiceDB containers"]
async fn docker_rustfs_spicedb_remote_snapshots_and_retirement() -> TestResult
{
    let storage = rustfs::start().await?;
    let (_spice_container, spice) = start_spice().await?;
    let project = project(256, false)?;
    let environment_a = environment(&project)?;
    let checked = compile(&project, &environment_a);
    let contract = checked
        .publishable()
        .ok_or_else(|| format!("{:?}", checked.issues()))?;
    let archive = compile_archive(&project, &environment_a)?;
    let next_project =
        fixture::project_with_document_title(260, false, "i64")?;
    let next = compile_archive(&next_project, &environment(&next_project)?)?;
    let local = ArchiveReader::open(archive.bytes(), archive.pin())?;
    let initial_token = spice.write(seed(&project)?).await?;
    let owner = Runtime {
        consistency: Consistency::AtLeastAsFresh(initial_token),
        ..runtime(contract)
    };
    let reader_role = Runtime {
        subject: "reader",
        ..owner.clone()
    };
    let select =
        |name, field, id| access(&project, name, field, id, Operation::Read);
    let title = select("Document", Some("title"), "d1")?;
    let body = select("Document", Some("body"), "d1")?;
    let document = select("Document", None, "d1")?;
    let options = RemoteOptions {
        reader_window: Duration::from_secs(3),
        publication_window: Duration::from_secs(3),
        timeout: Duration::from_secs(2),
        ..RemoteOptions::default()
    };
    let store = connect(&storage, options.clone()).await?;
    let generation = store.publish(&archive, None).await?;
    let anonymous = reqwest::get(format!(
        "{}/{}/{}",
        storage.endpoint,
        rustfs::BUCKET,
        artifact_key(generation)
    ))
    .await?;
    assert!(anonymous.status().is_client_error());

    let before = store.metrics();
    let remote = store.open().await?;
    let projection = remote
        .read(&title, &owner, &spice)
        .await
        .ok_or("remote title")?;
    assert_eq!(projection.target, title.route.target);
    assert_eq!(projection.primitive, Some(Primitive::String));
    let cold = store.metrics();
    assert!(cold.bytes - before.bytes < archive.bytes().len() as u64);
    assert!(cold.requests > before.requests);
    assert_eq!(remote.read(&title, &owner, &spice).await, Some(projection));
    let warm = store.metrics();
    assert_eq!(warm.requests, cold.requests);
    assert!(warm.ram_hits > cold.ram_hits);

    for (name, context, expected) in [
        (
            "hierarchical",
            Runtime {
                subject: "inherited",
                ..owner.clone()
            },
            true,
        ),
        (
            "nested members",
            Runtime {
                subject: "nested",
                ..owner.clone()
            },
            true,
        ),
        (
            "excluded",
            Runtime {
                subject: "excluded",
                ..owner.clone()
            },
            false,
        ),
        (
            "context denied",
            Runtime {
                region: "blocked",
                ..owner.clone()
            },
            false,
        ),
        (
            "missing context",
            Runtime {
                hour: None,
                ..owner.clone()
            },
            false,
        ),
    ] {
        let expected_local = local.read(&title, &context, &spice).await;
        assert_eq!(expected_local.is_some(), expected, "{name}");
        assert_eq!(
            remote.read(&title, &context, &spice).await,
            expected_local,
            "{name}"
        );
    }
    assert!(remote.read(&body, &reader_role, &spice).await.is_none());
    let mut transfer = access(
        &project,
        "Document",
        Some("body"),
        "d1",
        Operation::Transfer,
    )?;
    transfer.destination = "LLM".parse()?;
    assert_eq!(remote.read(&transfer, &owner, &spice).await, None);
    assert!(remote.read(&body, &owner, &spice).await.is_some());
    let index = select("RestrictedIndex", None, "i1")?;
    let mut full_context = owner.clone();
    dependencies(&project, &index, &mut full_context)?;
    let local_index = local
        .read(&index, &full_context, &spice)
        .await
        .ok_or("local view")?;
    assert_eq!(
        remote.read(&index, &full_context, &spice).await,
        Some(local_index)
    );
    let mut restricted = select("RestrictedIndex", None, "i1")?;
    restricted.destination = "LowClearance".parse()?;
    assert_eq!(local.read(&restricted, &full_context, &spice).await, None);
    assert_eq!(remote.read(&restricted, &full_context, &spice).await, None);
    assert!(
        remote
            .read(
                &index,
                &Runtime {
                    mfa: false,
                    ..full_context
                },
                &spice
            )
            .await
            .is_none()
    );

    let link = select("Message", Some("attachment"), "m1")?;
    let endpoints = [select("Document", None, "d1")?];
    let expected_local = local
        .links(&link, &endpoints, Direction::Direct, &owner, &spice)
        .await;
    assert_eq!(expected_local.as_ref().map(Vec::len), Some(1));
    assert_eq!(
        remote
            .links(&link, &endpoints, Direction::Direct, &owner, &spice)
            .await,
        expected_local
    );
    let inverse = [link];
    assert_eq!(
        remote
            .links(
                &document,
                &inverse,
                Direction::Inverse,
                &reader_role,
                &spice
            )
            .await,
        Some(Vec::new())
    );
    assert_eq!(
        remote
            .links(&document, &inverse, Direction::Inverse, &owner, &spice)
            .await,
        local
            .links(&document, &inverse, Direction::Inverse, &owner, &spice)
            .await
    );
    let mut foreign = select("Document", Some("title"), "d1")?;
    foreign.instance.tenant = "tenant_b".into();
    assert!(
        local
            .read(
                &foreign,
                &Runtime {
                    tenant: "tenant_b",
                    ..owner.clone()
                },
                &spice
            )
            .await
            .is_some()
    );
    assert!(
        remote
            .read(
                &foreign,
                &Runtime {
                    tenant: "tenant_b",
                    ..owner.clone()
                },
                &spice
            )
            .await
            .is_none()
    );

    assert!(remote.read(&title, &reader_role, &spice).await.is_some());
    let id = object(&document)?;
    let updates = vec![update(
        "OPERATION_DELETE",
        tuple(
            ("company_record", &id),
            "reader",
            ("user", "tenant_a/reader"),
            None,
        ),
    )];
    let (epoch, concurrent) = tokio::join!(
        spice.write(updates),
        remote.read(&title, &reader_role, &spice)
    );
    if let Some(record) = concurrent {
        assert_eq!(record.primitive, Some(Primitive::String));
    }
    let after = Runtime {
        consistency: Consistency::AtLeastAsFresh(epoch?),
        ..reader_role
    };
    let before = store.metrics();
    assert!(remote.read(&title, &after, &spice).await.is_none());
    assert_eq!(store.metrics().requests, before.requests);

    // Separate stores represent independent readers with no shared process
    // cache.
    let ssd = tempfile::tempdir()?;
    let other = connect(
        &storage,
        RemoteOptions {
            ram_bytes: 0,
            disk: Some(DiskCache {
                directory: ssd.path().into(),
                bytes: 2 * 1024 * 1024,
            }),
            ..options.clone()
        },
    )
    .await?;
    let left = store.open().await?;
    let right = other.open().await?;
    assert_eq!(left.generation(), right.generation());
    let right_title = right
        .read(&title, &owner, &spice)
        .await
        .ok_or("SSD title")?;
    assert_eq!(right.read(&title, &owner, &spice).await, Some(right_title));
    assert!(other.metrics().disk_hits > 0);

    let abandoned = store.stage(&archive, Some(generation)).await?;
    drop(abandoned);
    let (a, b) = tokio::join!(
        store.stage(&next, Some(generation)),
        other.stage(&next, Some(generation))
    );
    assert_eq!(store.active().await?, Some(generation));
    let (first, second, during) = tokio::join!(
        a?.activate(),
        b?.activate(),
        left.read(&title, &owner, &spice),
    );
    assert_eq!(
        during.and_then(|symbol| symbol.primitive),
        Some(Primitive::String)
    );
    let active = match (first, second) {
        (Ok(active), Err(RemoteError::Conflict)) |
        (Err(RemoteError::Conflict), Ok(active)) => active,
        outcome => {
            return Err(format!(
                "expected a single activated generation, got {outcome:?}"
            )
            .into());
        },
    };
    let newest = store.open().await?;
    assert_eq!(newest.generation(), active);
    assert_eq!(
        newest
            .read(&title, &owner, &spice)
            .await
            .and_then(|symbol| symbol.primitive),
        Some(Primitive::I64)
    );
    for previous in [&left, &right] {
        assert_eq!(previous.generation(), generation);
        assert_eq!(
            previous
                .read(&title, &owner, &spice)
                .await
                .and_then(|symbol| symbol.primitive),
            Some(Primitive::String)
        );
    }

    let cleanup = other.observe_cleanup().await?;
    assert_eq!(cleanup.sweep().await, Err(RemoteError::Retained));
    assert!(
        rustfs::object_keys(&storage.client)
            .await?
            .contains(&artifact_key(generation))
    );
    tokio::time::sleep(Duration::from_millis(6_050)).await;
    assert!(left.read(&title, &owner, &spice).await.is_none());
    assert!(right.read(&title, &owner, &spice).await.is_none());
    assert_eq!(cleanup.sweep().await?, 3);
    assert_eq!(cleanup.sweep().await?, 0);
    let objects = rustfs::object_keys(&storage.client).await?;
    assert_eq!(objects.len(), 2);
    assert!(objects.contains(&artifact_key(active)));

    let restarted = connect(&storage, options).await?;
    let reopened = restarted.open().await?;
    assert_eq!(reopened.generation(), active);
    assert_eq!(
        reopened
            .read(&title, &owner, &spice)
            .await
            .and_then(|symbol| symbol.primitive),
        Some(Primitive::I64)
    );

    storage
        .client
        .put_object()
        .bucket(rustfs::BUCKET)
        .key(artifact_key(active))
        .body(aws_sdk_s3::primitives::ByteStream::from_static(
            b"corruption",
        ))
        .send()
        .await?;
    let unprimed = connect(
        &storage,
        RemoteOptions {
            reader_window: Duration::from_secs(3),
            publication_window: Duration::from_secs(3),
            ram_bytes: 0,
            ..RemoteOptions::default()
        },
    )
    .await?;
    assert!(unprimed.open().await.is_err());
    storage
        .client
        .delete_object()
        .bucket(rustfs::BUCKET)
        .key(artifact_key(active))
        .send()
        .await?;
    assert!(matches!(unprimed.open().await, Err(RemoteError::Missing)));
    storage.container.stop().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "comparison benchmark using pinned RustFS and SpiceDB"]
async fn docker_rustfs_benchmark() -> TestResult {
    let storage = rustfs::start().await?;
    let (_spice_container, spice) = start_spice().await?;
    const SAMPLES: usize = 24;
    for size in [640, 3200] {
        let project = project(size, false)?;
        let configured = environment(&project)?;
        let checked = compile(&project, &configured);
        let contract = checked
            .publishable()
            .ok_or_else(|| format!("{:?}", checked.issues()))?;
        let archive = compile_archive(&project, &configured)?;
        let item =
            access(&project, "RestrictedIndex", None, "i1", Operation::Read)?;
        let anchor =
            access(&project, "Attached", None, "a1", Operation::Read)?;
        let endpoint =
            [access(&project, "Document", None, "d1", Operation::Read)?];
        let seed = spice.write(seed(&project)?).await?;
        let mut evidence = Runtime {
            consistency: Consistency::AtLeastAsFresh(seed),
            ..runtime(contract)
        };
        dependencies(&project, &item, &mut evidence)?;
        let local = ArchiveReader::open(archive.bytes(), archive.pin())?;
        let expected = local
            .read(&item, &evidence, &spice)
            .await
            .ok_or("expected local view")?;
        let expected_links = local
            .links(&anchor, &endpoint, Direction::Direct, &evidence, &spice)
            .await
            .ok_or("expected local links")?;
        let publisher = connect(&storage, RemoteOptions::default()).await?;
        let generation = publisher
            .publish(&archive, publisher.active().await?)
            .await?;
        let key = artifact_key(generation);
        let mut gate = Vec::new();
        for _ in 0..SAMPLES {
            let start = Instant::now();
            assert_eq!(
                authorize(contract, &item, &evidence, &spice).await,
                galadril_ontology::authorization::Decision::Allow
            );
            gate.push(start.elapsed());
        }
        println!(
            "size={size} archive={} gate_us={:?}",
            archive.bytes().len(),
            percentiles(&mut gate)?
        );

        let mut opened = Vec::new();
        let mut lookup = Vec::new();
        let mut links = Vec::new();
        let before = process_usage()?;
        let controls = publisher.metrics();
        for _ in 0..SAMPLES {
            let started = Instant::now();
            assert_eq!(publisher.active().await?, Some(generation));
            let bytes = storage
                .client
                .get_object()
                .bucket(rustfs::BUCKET)
                .key(&key)
                .send()
                .await?
                .body
                .collect()
                .await?
                .into_bytes();
            let reader = ArchiveReader::open(bytes.as_ref(), archive.pin())?;
            opened.push(started.elapsed());
            let start = Instant::now();
            assert_eq!(
                reader.read(&item, &evidence, &spice).await.as_ref(),
                Some(&expected)
            );
            lookup.push(start.elapsed());
            let start = Instant::now();
            assert_eq!(
                reader
                    .links(
                        &anchor,
                        &endpoint,
                        Direction::Direct,
                        &evidence,
                        &spice
                    )
                    .await
                    .as_ref(),
                Some(&expected_links)
            );
            links.push(start.elapsed());
        }
        let controls_after = publisher.metrics();
        print_row(
            "full",
            &mut opened,
            &mut lookup,
            &mut links,
            ReadMetrics {
                requests: SAMPLES as u64,
                bytes: archive.bytes().len() as u64 * SAMPLES as u64,
                manifest_requests: controls_after.manifest_requests -
                    controls.manifest_requests,
                manifest_bytes: controls_after.manifest_bytes -
                    controls.manifest_bytes,
                ..ReadMetrics::default()
            },
            before,
        )?;

        for (strategy, ram_bytes, parallelism) in [
            ("sparse", 0, 1),
            ("cached", 16 * 1024 * 1024, 1),
            ("grouped", 0, 4),
        ] {
            let store = connect(
                &storage,
                RemoteOptions {
                    ram_bytes,
                    parallelism,
                    ..RemoteOptions::default()
                },
            )
            .await?;
            let mut opened = Vec::new();
            let mut lookup = Vec::new();
            let mut links = Vec::new();
            let before = process_usage()?;
            for _ in 0..SAMPLES {
                let start = Instant::now();
                let reader = store.open().await?;
                opened.push(start.elapsed());
                let start = Instant::now();
                assert_eq!(
                    reader.read(&item, &evidence, &spice).await.as_ref(),
                    Some(&expected)
                );
                lookup.push(start.elapsed());
                let start = Instant::now();
                assert_eq!(
                    reader
                        .links(
                            &anchor,
                            &endpoint,
                            Direction::Direct,
                            &evidence,
                            &spice
                        )
                        .await
                        .as_ref(),
                    Some(&expected_links)
                );
                links.push(start.elapsed());
            }
            print_row(
                strategy,
                &mut opened,
                &mut lookup,
                &mut links,
                store.metrics(),
                before,
            )?;
            let reader = store.open().await?;
            let concurrent = Instant::now();
            let results = futures::future::join_all(
                (0..4).map(|_| reader.read(&item, &evidence, &spice)),
            )
            .await;
            assert!(
                results
                    .iter()
                    .all(|result| result.as_ref() == Some(&expected))
            );
            println!(
                "strategy={strategy} concurrent_4_us={}",
                concurrent.elapsed().as_micros()
            );
        }
    }
    Ok(())
}

fn percentiles(
    values: &mut [Duration],
) -> Result<[u128; 3], rustfs::TestError> {
    values.sort_unstable();
    let rank = |percent: usize| -> Result<u128, rustfs::TestError> {
        Ok(values
            .get((values.len() * percent).div_ceil(100).saturating_sub(1))
            .ok_or("missing benchmark sample")?
            .as_micros())
    };
    Ok([rank(50)?, rank(95)?, rank(99)?])
}

struct Usage {
    rss_kib: u64,
    cpu_ms: u64,
}

fn process_usage() -> Result<Usage, rustfs::TestError> {
    let output = std::process::Command::new("ps")
        .args([
            "-o",
            "rss=",
            "-o",
            "time=",
            "-p",
            &std::process::id().to_string(),
        ])
        .output()?;
    if !output.status.success() {
        return Err("ps failed".into());
    }
    let text = std::str::from_utf8(&output.stdout)?;
    let mut words = text.split_whitespace();
    let rss_kib = words.next().ok_or("missing RSS")?.parse()?;
    let time = words.next().ok_or("missing CPU time")?;
    let clock = time.split(':').try_fold(0.0, |total, part| {
        Ok::<_, rustfs::TestError>(total * 60.0 + part.parse::<f64>()?)
    })?;
    Ok(Usage {
        rss_kib,
        cpu_ms: (clock * 1000.0).round() as u64,
    })
}

fn print_row(
    strategy: &str,
    opened: &mut [Duration],
    lookup: &mut [Duration],
    links: &mut [Duration],
    counts: ReadMetrics,
    before: Usage,
) -> TestResult {
    let after = process_usage()?;
    println!(
        "strategy={strategy} open_us={:?} lookup_us={:?} links_us={:?} requests={} bytes={} range_requests={} range_bytes={} ram_hits={} disk_hits={} misses={} rss_kib={}->{} cpu_ms={}",
        percentiles(opened)?,
        percentiles(lookup)?,
        percentiles(links)?,
        counts.requests + counts.manifest_requests,
        counts.bytes + counts.manifest_bytes,
        counts.requests,
        counts.bytes,
        counts.ram_hits,
        counts.disk_hits,
        counts.misses,
        before.rss_kib,
        after.rss_kib,
        after.cpu_ms.saturating_sub(before.cpu_ms),
    );
    Ok(())
}
