# Follow-up: optional includes

Add an optional include directive: `include? <path>`. It behaves exactly like `include <path>`, except that when the file does not exist it is skipped silently. Every other error — a cycle, or a syntax error inside the included file — still raises `ConfigError` as before. Keep everything else in TASK.md working, and update your tests.
