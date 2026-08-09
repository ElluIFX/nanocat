"""NanoCat TUI visual theme: one CSS source for app, widgets and screens."""

THEME_CSS = """
/* ---- shell ------------------------------------------------------------ */
Screen {
    background: #0d1117;
}

#hdr {
    height: 1;
    width: 100%;
    background: #161b22;
    padding: 0 1;
}
#hdr Static { height: 1; }
#hdr-brand { color: #56d4dd; text-style: bold; width: auto; }
#hdr-session { color: #e6edf3; width: auto; max-width: 28; }
#hdr-model { color: #8b949e; width: auto; max-width: 34; }
#hdr-ctx { color: #8b949e; width: auto; }
#hdr-busy { color: #d29922; width: 2; }
#hdr-spacer { width: 1fr; }
#hdr Button {
    height: 1;
    min-width: 5;
    width: auto;
    border: none;
    background: #161b22;
    color: #8b949e;
    margin: 0 0 0 1;
    padding: 0 1;
}
#hdr Button:hover { background: #21262d; color: #e6edf3; }
#hdr Button:focus { text-style: bold; }
#hdr-logs.-active { color: #56d4dd; }
#hdr-sessions-btn.-active { color: #56d4dd; }

/* ---- sessions sidebar -------------------------------------------------- */
#sessions-pane {
    width: 30;
    background: #10151c;
    border-right: solid #2d333b;
}
#sb-new {
    width: 100%;
    height: 1;
    border: none;
    background: #1c2128;
    color: #56d4dd;
    margin: 0;
}
#sb-new:hover { background: #21262d; }
#sb-filter {
    height: 1;
    border: none;
    background: #0d1117;
    color: #e6edf3;
    padding: 0 1;
}
#sb-list { height: 1fr; background: #10151c; }
#sb-list ListItem { padding: 0 1; height: 2; }
#sb-list ListItem.--highlight { background: #1c2128; }
.session-name { color: #e6edf3; }
.session-meta { color: #8b949e; }
.session-active .session-name { color: #56d4dd; text-style: bold; }

/* ---- chat pane --------------------------------------------------------- */
#chat-pane { width: 1fr; }
#approvals { height: auto; max-height: 14; overflow-y: auto; padding: 0 1; }
.approval-card {
    height: auto;
    border: round #d29922;
    background: #161b22;
    padding: 1;
    margin: 0 0 1 0;
}
.approval-details { height: auto; }
.approval-status { height: auto; margin: 1 0 0 0; }
.approval-actions { height: 3; margin: 1 0 0 0; }
.approval-actions Button { margin: 0 1 0 0; min-width: 14; }
#chat { height: 1fr; padding: 0 1; overflow-x: hidden; background: #0d1117; }
#activity { height: 1; color: #8b949e; padding: 0 1; background: #10151c; }
#prompt { border: none; height: 4; padding: 0 1; background: #10151c; }
#composer-bar {
    height: 1;
    width: 100%;
    padding: 0 1;
    background: #161b22;
}
#composer-bar Button {
    height: 1;
    border: none;
    width: auto;
    min-width: 6;
    margin: 0 1 0 0;
    padding: 0 1;
    background: #161b22;
}
#composer-spacer { width: 1fr; min-width: 0; }
#attach { color: #8b949e; }
#attach:hover { color: #e6edf3; }
#send { margin: 0; }
#send.-send { color: #3fb950; }
#send.-stop { color: #f85149; }
#send.-steer { color: #d29922; }
#approval-mode.-auto { color: #3fb950; }
#approval-mode.-yolo { color: #f85149; text-style: bold; }

/* ---- log drawer (hidden by default) & status panel ---------------------- */
#right-pane {
    width: 42;
    background: #10151c;
    border-left: solid #2d333b;
}
#status-pane { padding: 0 1; }
#sp-title { color: #8b949e; text-style: bold; margin: 1 0 1 0; }
#status-pane Static { height: 1; }
#sp-spacer { height: 1fr; }
#sp-hint { color: #8b949e; }
#log-pane {
    display: none;
    height: 1fr;
}
#right-pane.-logs #log-pane { display: block; }
#right-pane.-logs #status-pane { display: none; }
#log-bar { height: 1; padding: 0 1; background: #161b22; }
#log-title { color: #8b949e; width: auto; }
#log-bar Input {
    width: 1fr;
    height: 1;
    border: none;
    background: #0d1117;
    padding: 0 1;
}
#log-bar Select { width: 12; height: 1; border: none; }
#log-bar Button {
    height: 1;
    min-width: 3;
    width: auto;
    border: none;
    background: #161b22;
    color: #8b949e;
    padding: 0 1;
    margin: 0 0 0 1;
}
#log-bar Button:hover { color: #e6edf3; }
#log { height: 1fr; padding: 0 1; overflow-x: hidden; }
#hdr-logs.-notify { color: #d29922; }

/* ---- narrow layout ------------------------------------------------------ */
.narrow #sessions-pane { width: 24; }
.narrow #right-pane { display: none; }
.narrow #right-pane.-logs {
    display: block;
    layer: overlay;
    dock: right;
    width: 60%;
}
.narrow #hdr-model { display: none; }

/* ---- status animations --------------------------------------------------- */
.approval-card.-pulse { border: round #f85149; }

/* ---- modal screens ------------------------------------------------------ */
ConfirmScreen, ResultScreen, ActionFormScreen, SessionDetailScreen,
ModelManagerScreen, ModelEditScreen {
    align: center middle;
}
.dialog {
    width: 64;
    max-width: 90%;
    height: auto;
    max-height: 80%;
    padding: 1 2;
    border: round #2d333b;
    background: #161b22;
}
.dialog-tall { height: 70%; }
.dialog-tall .dialog-body { height: 1fr; }
.dialog-title { text-style: bold; color: #e6edf3; margin-bottom: 1; }
.dialog-danger { border: round #f85149; }
.dialog-buttons { height: auto; align-horizontal: right; margin-top: 1; }
.dialog-buttons Button { margin: 0 0 0 1; min-width: 10; }
.dialog-body { height: auto; max-height: 24; }
.dialog Input { margin: 0 0 1 0; }
.dialog Select { margin: 0 0 1 0; }
.dialog Checkbox { margin: 0 0 1 0; }
.field-label { color: #8b949e; height: 1; }
.dialog-status { height: auto; color: #d29922; margin-top: 1; }

ActionCenterScreen { align: center top; }
#action-center {
    width: 72;
    max-width: 92%;
    height: auto;
    max-height: 70%;
    margin-top: 3;
    padding: 1 2;
    border: round #7c6cff;
    background: #161b22;
}
#ac-search { border: none; background: #0d1117; margin-bottom: 1; }
#ac-list { height: auto; max-height: 30; background: #161b22; }
#ac-list .option-list--option-highlighted { background: #1c2128; }
.ac-group { color: #7c6cff; text-style: bold; }
#ac-hint { color: #8b949e; height: 1; margin-top: 1; }

SettingsScreen { align: center middle; }
#settings-dialog {
    width: 76;
    max-width: 94%;
    height: 86%;
    max-height: 90%;
    padding: 1 2;
    border: round #7c6cff;
    background: #161b22;
}
#settings-scroll { height: 1fr; min-height: 8; overflow-y: auto; }
.settings-section { color: #56d4dd; text-style: bold; margin: 1 0 0 0; }
.settings-row { height: auto; margin: 0 0 1 0; }
.settings-row Label { width: 14; color: #8b949e; padding-top: 1; }
.settings-row Select { width: 1fr; }
.settings-note { color: #8b949e; height: auto; }
#settings-status { height: auto; color: #d29922; margin-top: 1; }
#mm-list { height: 1fr; min-height: 6; }
"""
