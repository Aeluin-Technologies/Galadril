//! Ordered scopes with duplicate-safe construction and borrowed reads.

use std::collections::BTreeMap;
use std::collections::btree_map::Entry;

use crate::error::ModelError;
use crate::identity::Name;

/// A scope of uniquely named entries, iterated in canonical name order.
#[cfg_attr(feature = "archive", derive(serde::Serialize))]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Named<T>(BTreeMap<Name, T>);

impl<T> Named<T> {
    /// Creates an empty scope without allocating a map node.
    #[must_use]
    pub fn new() -> Self {
        Self(BTreeMap::new())
    }

    /// Inserts an entry without replacing a previous declaration.
    ///
    /// # Errors
    ///
    /// Returns [`ModelError::DuplicateName`] if the name is already present.
    pub fn insert(&mut self, name: Name, value: T) -> Result<(), ModelError> {
        match self.0.entry(name) {
            Entry::Vacant(entry) => {
                entry.insert(value);
                Ok(())
            },
            Entry::Occupied(entry) => Err(ModelError::DuplicateName {
                name: entry.key().clone(),
            }
            .record()),
        }
    }

    /// Borrows the entry by its exact spelling.
    #[must_use]
    pub fn get(&self, name: &str) -> Option<&T> {
        self.0.get(name)
    }

    /// Iterates in ascending, case-sensitive name order.
    pub fn iter(&self) -> impl ExactSizeIterator<Item = (&Name, &T)> + '_ {
        self.0.iter()
    }

    /// Counts the entries in this scope.
    #[must_use]
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// Reports whether the scope has any entries.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl<T> Default for Named<T> {
    fn default() -> Self {
        Self::new()
    }
}
