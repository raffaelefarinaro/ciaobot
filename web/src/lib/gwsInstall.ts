// Kept in step with GWS_INSTALL_COMMAND in ciao/host_terminal.py
// (tests/test_host_terminal.py asserts they match). npm 12 blocks install scripts by
// default, and the package's postinstall is what downloads the gws binary.
export const GWS_INSTALL_COMMAND = 'npm install -g --allow-scripts=@googleworkspace/cli @googleworkspace/cli'
