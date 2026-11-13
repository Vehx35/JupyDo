# JupyDo self-test

Checks that the environment works: its kernels, sudo, compilers, storage, network and,
when the environment has them, Apptainer, the job queue and GPUs. Checks that do not
apply say SKIP.

Two ways to run it:
- open `jupydo_selftest.ipynb` and run its cell (needs a Python kernel);
- or, in any environment (also without Python), in a terminal: `jupydo-selftest`

If something says FAIL, send the output to your JupyDo administrator.
