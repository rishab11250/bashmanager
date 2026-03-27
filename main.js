const { app, BrowserWindow } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');

let mainWindow;
let flaskProcess;

const PORT = 5000;
const HOST = '127.0.0.1';

// We must preserve the python path to our bundled app or local python
const pythonCmd = process.env.VIRTUAL_ENV
    ? path.join(process.env.VIRTUAL_ENV, 'bin', 'python')
    : (process.platform === 'win32' ? 'python' : 'python3');

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1200,
        height: 800,
        minWidth: 900,
        minHeight: 600,
        title: "DevShell",
        autoHideMenuBar: true, // Hide menu bar
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true
        }
    });

    // We keep trying to load the URL until Flask is ready
    loadWhenReady(`http://${HOST}:${PORT}`);

    mainWindow.on('closed', function () {
        mainWindow = null;
    });
}

function startFlaskServer() {
    console.log(`Starting Python server... (using ${pythonCmd})`);

    flaskProcess = spawn(pythonCmd, ['app.py'], {
        cwd: __dirname
    });

    flaskProcess.stdout.on('data', (data) => {
        console.log(`Flask: ${data}`);
    });

    flaskProcess.stderr.on('data', (data) => {
        console.error(`Flask Err: ${data}`);
    });

    flaskProcess.on('close', (code) => {
        console.log(`Flask process exited with code ${code}`);
    });
}

// Poller to check if Flask is up
function loadWhenReady(url) {
    const checkServer = () => {
        http.get(url, (res) => {
            if (res.statusCode === 200) {
                console.log('Server is ready. Loading UI...');
                mainWindow.loadURL(url);
            } else {
                setTimeout(checkServer, 200);
            }
        }).on('error', (err) => {
            console.log('Waiting for server...');
            setTimeout(checkServer, 200);
        });
    };

    checkServer();
}

app.on('ready', () => {
    startFlaskServer();
    createWindow();
});

// Quit when all windows are closed.
app.on('window-all-closed', function () {
    if (process.platform !== 'darwin') {
        app.quit();
    }
});

app.on('activate', function () {
    if (mainWindow === null) {
        createWindow();
    }
});

// Ensure python child process is killed when app exits
app.on('will-quit', () => {
    if (flaskProcess) {
        flaskProcess.kill();
    }
});
