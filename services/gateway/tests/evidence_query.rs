//! Time and selector validation happens before tenant evidence is queried.

#[path = "../src/domain/evidence.rs"]
mod evidence;

#[test]
fn evidence_queries_require_a_bounded_period_and_well_formed_filters()
-> anyhow::Result<()> {
    use evidence::EvidenceFilter;
    let filter: EvidenceFilter = serde_json::from_value(
        serde_json::json!({"fromMs":0,"untilMs":1000,"recipient":"Person@Example.org"}),
    )?;
    assert_eq!(
        filter.validated()?.recipient.as_deref(),
        Some("person@example.org")
    );
    for value in [
        serde_json::json!({"fromMs":1000,"untilMs":0}),
        serde_json::json!({"fromMs":-1,"untilMs":1000}),
        serde_json::json!({"fromMs":0,"untilMs":1000,"sender":"a@example.org\nBcc:x@example.org"}),
        serde_json::json!({"fromMs":0,"untilMs":1000,"ontologyId":" "}),
    ] {
        assert!(
            serde_json::from_value::<EvidenceFilter>(value)?
                .validated()
                .is_err()
        );
    }
    assert!(
        serde_json::from_value::<EvidenceFilter>(
            serde_json::json!({"fromMs":0,"untilMs":1,"tenant":"other"})
        )
        .is_err()
    );
    Ok(())
}

#[test]
fn evidence_projection_retains_event_provenance_and_rank_status()
-> anyhow::Result<()> {
    let row: evidence::EvidenceRow = serde_json::from_value(
        serde_json::json!({"sourceId":"mail","eventId":"event","eventTimeMs":1,"unitId":"a".repeat(64),"title":"Subject","text":"Body","metadata":{},"facts":[{"subject":"Alice","predicate":"sent","object":"message"}]}),
    )?;
    let page = evidence::EvidencePage {
        items: vec![row],
        after: None,
        engine: "chronology".into(),
        warnings: vec![],
    };
    assert_eq!(
        serde_json::to_value(page)?.pointer("/items/0/eventId"),
        Some(&serde_json::json!("event"))
    );
    Ok(())
}
