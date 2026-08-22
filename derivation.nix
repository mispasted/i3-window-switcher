{
    lib,
    python3Packages
}:

python3Packages.buildPythonApplication(finalAttrs: {
    pname = "i3-window-switcher";
    version = "2.0";

    src = ./.;

    pyproject = true;

    build-system = with python3Packages; [ setuptools ];

    dependencies = with python3Packages; [
        i3ipc
    ];
})