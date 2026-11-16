//! Computation labels follow observed inputs rather than subject clearance.

use galadril_ontology::Classification;
use galadril_ontology::authorization::{
    Clearance, Computation, Flow, FlowError,
};

#[test]
fn clearance_does_not_taint_an_independent_public_calculation()
-> Result<(), Box<dyn std::error::Error>> {
    let ceiling = Clearance::new(Classification::Restricted);
    let mut context =
        Computation::from_inputs(ceiling, Flow::new(Classification::Public))?;
    context.check_write(
        &Flow::new(Classification::Public),
        &"Internal".parse()?,
        &"Edit".parse()?,
    )?;
    context.observe(&Flow::new(Classification::Restricted))?;
    assert_eq!(
        context.check_write(
            &Flow::new(Classification::Public),
            &"Internal".parse()?,
            &"Edit".parse()?
        ),
        Err(FlowError::WriteDown)
    );
    context.check_write(
        &Flow::new(Classification::Restricted),
        &"Internal".parse()?,
        &"Edit".parse()?,
    )?;
    Ok(())
}

#[test]
fn failed_observations_cannot_be_used_as_public_control_inputs()
-> Result<(), Box<dyn std::error::Error>> {
    let ceiling = Clearance::new(Classification::Internal);
    let mut context =
        Computation::from_inputs(ceiling, Flow::new(Classification::Public))?;
    assert_eq!(
        context.observe(&Flow::new(Classification::Confidential)),
        Err(FlowError::Clearance)
    );
    assert!(context.current_label().is_err());
    assert_eq!(
        context.check_write(
            &Flow::new(Classification::Public),
            &"Internal".parse()?,
            &"Edit".parse()?
        ),
        Err(FlowError::Poisoned)
    );
    Ok(())
}

#[test]
fn compartments_and_release_restrictions_follow_observed_values()
-> Result<(), Box<dyn std::error::Error>> {
    let mut clearance = Clearance::new(Classification::Restricted);
    clearance.compartments.insert("Legal".parse()?);
    let mut context = Computation::from_inputs(
        clearance,
        Flow::new(Classification::Public),
    )?;
    let mut legal = Flow::new(Classification::Confidential);
    legal.compartments.insert("Legal".parse()?);
    legal.destinations = Some(["Internal".parse()?].into());
    legal.purposes = Some(["Review".parse()?].into());
    context.observe(&legal)?;
    assert_eq!(
        context.check_write(&legal, &"LLM".parse()?, &"Review".parse()?),
        Err(FlowError::Restriction)
    );
    assert_eq!(
        context.check_write(
            &legal,
            &"Internal".parse()?,
            &"Training".parse()?
        ),
        Err(FlowError::Restriction)
    );
    context.check_write(&legal, &"Internal".parse()?, &"Review".parse()?)?;
    let mut unclassified_compartment = Flow::new(Classification::Restricted);
    unclassified_compartment
        .compartments
        .insert("Finance".parse()?);
    assert_eq!(
        context.observe(&unclassified_compartment),
        Err(FlowError::Clearance)
    );
    Ok(())
}

#[test]
fn generated_label_joins_preserve_the_full_partial_order()
-> Result<(), Box<dyn std::error::Error>> {
    fn label(bits: u16) -> Result<Flow, Box<dyn std::error::Error>> {
        let mut flow = Flow::new(match bits & 3 {
            0 => Classification::Public,
            1 => Classification::Internal,
            2 => Classification::Confidential,
            _ => Classification::Restricted,
        });
        for (index, name) in
            ["Legal", "Finance", "Medical"].into_iter().enumerate()
        {
            if bits & (1 << (index + 2)) != 0 {
                flow.compartments.insert(name.parse()?);
            }
        }
        for (selector, destination) in [(bits >> 5, true), (bits >> 7, false)]
        {
            let restriction = match selector & 3 {
                0 => None,
                1 => Some(Default::default()),
                2 => Some(["Internal".parse()?].into()),
                _ => Some(["Internal".parse()?, "Audit".parse()?].into()),
            };
            if destination {
                flow.destinations = restriction;
            } else {
                flow.purposes = restriction;
            }
        }
        Ok(flow)
    }
    let labels = (0..512).map(label).collect::<Result<Vec<_>, _>>()?;
    for (index, a) in labels.iter().enumerate() {
        assert_eq!(a.join(a), *a);
        for (offset, b) in labels.iter().enumerate() {
            let joined = a.join(b);
            assert_eq!(joined, b.join(a));
            assert!(joined.dominates(a) && joined.dominates(b));
            let c = labels
                .get((index * 17 + offset * 31) % labels.len())
                .ok_or("generator index")?;
            assert_eq!(joined.join(c), a.join(&b.join(c)));
            if c.dominates(a) && c.dominates(b) {
                assert!(c.dominates(&joined));
            }
        }
    }
    Ok(())
}
