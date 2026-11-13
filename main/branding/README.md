# Branding and terms of this installation

Replace these files with your own; they are read by the hub (`compose.yaml` mounts
this folder read-only at `/etc/jupydo-branding`):

- `logo.png`  - logo of the hub pages and of JupyterLab (restart the hub after changing it)
- `terms.pdf` - terms of use every user must accept. The shipped file is only a TEMPLATE:
  replace it with your organization's text. A new file is a new version: every user
  must accept it again at the next login. Delete it to ask for no terms at all.

Who accepted which version is recorded in `terms_acceptances.jsonl` in the hub data
folder (JUPYDO_DATA_PATH), never here.
