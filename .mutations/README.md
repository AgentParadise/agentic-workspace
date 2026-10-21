# Mutations for the headless Claude Code transport

Each patch re-introduces one bug in
`lib/python/agentic_isolation/agentic_isolation/harnesses/claude/headless.py`.
Named test file: `lib/python/agentic_isolation/tests/test_claude_headless.py`.

| Patch | Bug | Killing test |
|---|---|---|
| 01-model-alias-not-exact-id | `--model` gets an alias (`sonnet`) instead of the exact id | `test_runs_the_cli_headless_with_the_exact_model` |
| 02-alias-guard-dropped | an alias reaches the CLI, which picks the latest model | `test_a_model_alias_is_refused_before_any_call` |
| 03-answer-stdin-left-open | the answer runs with stdin inherited | `test_closes_stdin_and_passes_its_timeout` |
| 04-reported-error-swallowed | an `is_error` envelope is returned as an answer | `test_every_failure_raises_its_own_error[is-error]` |
| 05-nonzero-exit-swallowed | a non-zero exit is read as output | `test_every_failure_raises_its_own_error[exit]` |
| 06-api-key-inherited | ANTHROPIC_API_KEY reaches the CLI, which then bills the API | `test_no_credential_in_the_parent_environment_reaches_the_cli` |
| 07-setting-sources-unrestricted | `--setting-sources user` dropped, so project/local settings load | `test_runs_the_cli_headless_with_the_exact_model`, real-CLI `test_no_settings_file_routes_the_real_cli_off_the_oauth_token[project,local]` |
| 08-oauth-token-source-unchecked | a settings-file bearer token passes for the OAuth token | `test_a_settings_bearer_token_cannot_pass_for_oauth` |
| 09-api-key-auth-accepted | an `authMethod` outside the OAuth set is accepted | `test_a_call_that_would_not_run_on_oauth_is_refused_before_any_answer[non-oauth-method-without-key-source]` |
| 10-model-alias-not-exact-id-after-end-of-options | same attack as 01, written when the prompt moved after `--` | `test_runs_the_cli_headless_with_the_exact_model` |
| 11-setting-sources-unrestricted-after-end-of-options | same attack as 07, written when the prompt moved after `--` | `test_runs_the_cli_headless_with_the_exact_model`, real-CLI `test_no_settings_file_routes_the_real_cli_off_the_oauth_token[project,local]` |
| 12-prompt-before-end-of-options | the `--` is dropped, so a prompt such as `--help` is parsed as a CLI option | `test_runs_the_cli_headless_with_the_exact_model`, `test_a_flag_shaped_prompt_is_passed_after_the_end_of_options`, real-CLI `test_the_real_cli_sends_a_flag_shaped_prompt_as_the_prompt` |

Do not delete before the trusted mutation-evidence run posts PASS for the head SHA (PC-204).
