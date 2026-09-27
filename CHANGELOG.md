# Changelog

## 4.0.0

### Breaking contracts

- Model requests use action_id for the action kind and call_id for the invocation.
- Invocation results use call_result. Call IDs remain unique across restored contexts.
- The new context format does not migrate legacy memories automatically.

### Runtime and SDK

- Independent capability executions, exactly-once finalization and caller-directed errors.
- Global running/paused state is separate from local capability assignments.
- Streaming generations can be interrupted; agent and preset deletion clean up descendants.
- Protected singleton presets, persistent contexts and non-destructive restore failures.
- Attachment names survive transport and restore; filename collisions preserve history.

### Built-in features

- Exact-match automation with create/list/edit/remove actions and normal dispatch.
- Preset create/edit/remove and messaging between agents.
- Packaged main/module_manager defaults and self-contained capability SDK instructions.
- Core file and bash development tools, without requiring preinstalled user extensions.
- Environment-driven reasoning effort; an empty value uses the provider default.
- Text reply only when the configured external s2 binary is absent.

### Distribution

- Packaged prompts and voice references, environment initialization and model preparation.
- CPU defaults, optional CUDA dependencies, isolated wheel smoke checks and CI.
- Generic architecture/SDK documentation and GPL-3.0-only licensing.

Voice devices, provider-side cancellation and real model quality depend on the
deployment. Automated tests use synthetic APIs and temporary storage.
