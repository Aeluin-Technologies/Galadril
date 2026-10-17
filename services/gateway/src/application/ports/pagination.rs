//! Application pages carry only authorized values and their continuation
//! cursor.

pub struct Page<T> {
    pub items: Vec<T>,
    pub after: Option<String>,
}
