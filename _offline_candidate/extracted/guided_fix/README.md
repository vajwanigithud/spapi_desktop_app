# Guided child authorization — offline candidate, not production-approved

This bundle contains only staged replacements for seven app files and one
independent offline safety test. The uploaded originals were not overwritten.
Do not deploy or enable the scheduler or submit a child PATCH from this bundle.

Offline check: `python -m unittest discover -s guided_fix/tests -p test_child_authorization_unit.py -v`

Before deployment on Windows, merge the changed response-shape fixtures into
`tests/test_parent_child_guided_mode.py`, run its full isolated pytest suite
and an end-to-end mocked restart/scheduler rehearsal, inspect the production
migration state, and confirm no other backend instance can process the job.
The old tests expect GET fixtures lacking returned ASINs, and the old rehearsal
expects automatic child PATCH without explicit authorization; they cannot be
considered conclusive for the new gate.
