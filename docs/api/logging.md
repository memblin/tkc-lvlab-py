# tkc_lvlab._logging

Centralized project logger configuration. `lvlab`'s root Click group
calls `configure_logging()` once at startup, translating `-v` / `-q`
into a level on the project root logger.

::: tkc_lvlab._logging
