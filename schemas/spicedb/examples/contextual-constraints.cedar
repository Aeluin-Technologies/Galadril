// Cedar narrows an existing SpiceDB allow. Times are UTC and all attributes
// are derived from signed identity claims by Gateway.
permit(principal, action, resource)
when { context.is_structural_allowed == true };

forbid(principal, action, resource)
when {
  context.role == "data_engineer" &&
  (context.region != "Europe" || context.hour_utc < 8 || context.hour_utc >= 19)
};

forbid(principal, action, resource)
when {
  context.entity_id == "person1" && context.internal_device == false
};
