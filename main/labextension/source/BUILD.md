# jupydo-notifications (JupyterLab 4 extension)

Admin notifications as a bar at the top of JupyterLab, updated every 30 s.
The built extension is in `../jupydo-notifications/` (that is what JupyDo mounts).

To rebuild after editing `lib/index.js`:

    npm install
    STAGING=$(python3 -c "import jupyterlab,os;print(os.path.join(os.path.dirname(jupyterlab.__file__),'staging'))")
    node node_modules/@jupyterlab/builder/lib/build-labextension.js --core-path "$STAGING" .
    rm -rf ../jupydo-notifications && cp -r labextension ../jupydo-notifications
