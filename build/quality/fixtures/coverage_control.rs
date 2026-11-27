fn clamp_nonnegative(value: i32) -> i32 {
    if value < 0 {
        return 0;
    }

    value
}

#[test]
fn negative_values_become_zero() {
    assert_eq!(clamp_nonnegative(-1), 0);
}

#[test]
fn nonnegative_values_are_preserved() {
    assert_eq!(clamp_nonnegative(0), 0);
    assert_eq!(clamp_nonnegative(7), 7);
}
