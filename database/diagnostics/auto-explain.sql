-- Load into a single administrator session against the isolated diagnostic database.
LOAD 'auto_explain';
SET auto_explain.log_min_duration = '250ms';
SET auto_explain.sample_rate = 0.1;
SET auto_explain.log_format = 'json';
SET auto_explain.log_analyze = on;
SET auto_explain.log_buffers = on;
SET auto_explain.log_timing = off;
SET auto_explain.log_parameter_max_length = 0;
SET auto_explain.log_nested_statements = off;
