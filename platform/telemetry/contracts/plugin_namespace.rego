package after_resolution

import rego.v1

plugin_schema_prefix := "https://galadril.aeluin.com/schemas/plugins/"
plugin_id := split(trim_prefix(input.schema_url, plugin_schema_prefix), "/")[0]
plugin_namespace := concat(".", ["galadril.plugin", plugin_id, ""])

deny contains finding("plugin_schema", input.schema_url, "") if {
    not startswith(input.schema_url, plugin_schema_prefix)
}

deny contains finding("plugin_schema", input.schema_url, "") if {
    not regex.match("^[a-z][a-z0-9_]*$", plugin_id)
}

deny contains finding("plugin_span_namespace", span.type, "") if {
    some span in input.registry.spans
    not startswith(span.type, plugin_namespace)
}

deny contains finding("plugin_span_name", span.type, "") if {
    some span in input.registry.spans
    some template in span.name.templates
    template != span.type
}

deny contains finding("plugin_event_namespace", event.name, "") if {
    some event in input.registry.events
    not startswith(event.name, plugin_namespace)
}

deny contains finding("plugin_metric_namespace", metric.name, "") if {
    some metric in input.registry.metrics
    not startswith(metric.name, plugin_namespace)
}

deny contains finding("plugin_attribute_namespace", attr.key, attr.key) if {
    some attr in input.registry.attributes
    not startswith(attr.key, plugin_namespace)
}

deny contains finding("plugin_metric_cardinality", metric.name, attr.key) if {
    some metric in input.registry.metrics
    some attr in metric.attributes
    not bounded_operational(attr)
}

bounded_operational(attr) if {
    attr.annotations["galadril.cardinality"] == "bounded"
    attr.annotations["galadril.classification"] == "operational"
    count(attr.type.members) <= 32
}

finding(id, group, attr) := {
    "id": id,
    "type": "semconv_attribute",
    "category": "attribute",
    "group": group,
    "attr": attr,
}
