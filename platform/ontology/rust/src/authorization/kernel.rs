//! Executable scalar IFC rules shared unchanged with the Verus harness.

#[cfg(verus_keep_ghost)]
use vstd::prelude::*;

/// Selects the least classification that protects both inputs.
#[cfg_attr(verus_keep_ghost, verus_verify)]
#[cfg_attr(verus_keep_ghost, verus_spec(result =>
    ensures result >= left, result >= right, result == left || result == right,
))]
pub fn join_level(left: u8, right: u8) -> u8 {
    if left >= right { left } else { right }
}

/// Raises the computation only when its complete observed level fits
/// clearance.
#[cfg_attr(verus_keep_ghost, verus_verify)]
#[cfg_attr(verus_keep_ghost, verus_spec(result =>
    ensures
        result.is_some() == (current <= clearance && data <= clearance),
        match result {
            Some(level) => level >= current && level >= data && level <= clearance
                && (level == current || level == data),
            None => true,
        },
))]
pub fn read_level(current: u8, data: u8, clearance: u8) -> Option<u8> {
    let next = join_level(current, data);
    if next <= clearance { Some(next) } else { None }
}

/// Compares information actually used by a calculation with its destination.
#[cfg_attr(verus_keep_ghost, verus_verify)]
#[cfg_attr(verus_keep_ghost, verus_spec(result => ensures result == (current <= destination),))]
pub fn write_level(current: u8, destination: u8) -> bool {
    current <= destination
}

/// A successful release must satisfy every independently required authority.
#[cfg_attr(verus_keep_ghost, verus_verify)]
#[cfg_attr(verus_keep_ghost, verus_spec(result =>
    ensures result == (relationship && contextual && flow),
        result ==> relationship, result ==> contextual, result ==> flow,
))]
pub fn obligations(relationship: bool, contextual: bool, flow: bool) -> bool {
    relationship && contextual && flow
}

/// Numeric state is private so callers cannot lower an observed label.
#[cfg_attr(verus_keep_ghost, verus_verify)]
#[cfg_attr(not(verus_keep_ghost), derive(Debug))]
pub struct State {
    pub(crate) current: u8,
    pub(crate) maximum: u8,
    pub(crate) poisoned: bool,
}

#[cfg_attr(verus_keep_ghost, verus_verify)]
#[cfg_attr(verus_keep_ghost, verus_spec(result => ensures
    result.is_some() == (current <= maximum),
    match result { Some(state) => state.current == current && state.maximum == maximum && !state.poisoned, None => true },
))]
pub(crate) fn new_state(current: u8, maximum: u8) -> Option<State> {
    if current <= maximum {
        Some(State {
            current,
            maximum,
            poisoned: false,
        })
    } else {
        None
    }
}

#[cfg_attr(verus_keep_ghost, verus_verify)]
#[cfg_attr(verus_keep_ghost, verus_spec(result => ensures
    final(state).maximum == old(state).maximum,
    final(state).current >= old(state).current,
    old(state).current <= old(state).maximum ==> final(state).current <= final(state).maximum,
    old(state).poisoned ==> final(state).poisoned,
    result == (!old(state).poisoned && dimensions && old(state).current <= old(state).maximum && data <= old(state).maximum),
    result ==> final(state).current >= data && !final(state).poisoned,
    !result ==> final(state).poisoned && final(state).current == old(state).current,
))]
pub(crate) fn observe_state(
    state: &mut State,
    data: u8,
    dimensions: bool,
) -> bool {
    if state.poisoned || !dimensions {
        state.poisoned = true;
        return false;
    }
    match read_level(state.current, data, state.maximum) {
        Some(level) => {
            state.current = level;
            true
        },
        None => {
            state.poisoned = true;
            false
        },
    }
}

#[cfg_attr(verus_keep_ghost, verus_verify)]
#[cfg_attr(verus_keep_ghost, verus_spec(result => ensures
    result == (!state.poisoned && state.current <= state.maximum && state.current <= destination && dimensions),
))]
pub(crate) fn may_write_state(
    state: &State,
    destination: u8,
    dimensions: bool,
) -> bool {
    !state.poisoned &&
        state.current <= state.maximum &&
        write_level(state.current, destination) &&
        dimensions
}

#[cfg_attr(verus_keep_ghost, verus_verify)]
#[cfg_attr(verus_keep_ghost, verus_spec(ensures
    final(state).poisoned, final(state).current == old(state).current, final(state).maximum == old(state).maximum,
))]
pub(crate) fn poison_state(state: &mut State) {
    state.poisoned = true;
}

#[cfg_attr(verus_keep_ghost, verus_verify)]
#[cfg_attr(verus_keep_ghost, verus_spec(result => ensures result == state.poisoned,))]
pub(crate) fn is_poisoned_state(state: &State) -> bool {
    state.poisoned
}
