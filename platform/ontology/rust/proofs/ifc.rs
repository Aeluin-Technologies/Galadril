//! Verify the same executable scalar rules imported by the production gate.

use vstd::prelude::*;

#[path = "../src/authorization/kernel.rs"]
mod kernel;

#[verus_verify]
#[verus_spec(result => ensures result)]
fn commutative(a: u8, b: u8) -> bool {
    let ab = kernel::join_level(a, b);
    let ba = kernel::join_level(b, a);
    ab == ba
}

#[verus_verify]
fn lattice_laws(a: u8, b: u8, c: u8) {
    let same = kernel::join_level(a, a);
    let ab = kernel::join_level(a, b);
    let bc = kernel::join_level(b, c);
    let left = kernel::join_level(ab, c);
    let right = kernel::join_level(a, bc);
    proof! { assert(same == a); assert(left == right); }
}

#[verus_verify]
fn independent_clearance_is_not_a_source(maximum: u8, destination: u8) {
    let low = kernel::read_level(0, 0, maximum);
    let allowed = kernel::write_level(0, destination);
    proof! { assert(low == Some(0)); assert(allowed); }
}

fn main() {}
