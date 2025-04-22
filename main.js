import * as duckdb from '@duckdb/duckdb-wasm';
import duckdb_wasm from '@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm?url';
import mvp_worker from '@duckdb/duckdb-wasm/dist/duckdb-browser-mvp.worker.js?url';
import duckdb_wasm_next from '@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url';
import eh_worker from '@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?url';

// For rendering EC2 Table
import 'datatables.net-responsive-dt';

// Load static DuckDB database from GitHub
import dbfile from './static/cloudspecs.duckdb?url';


// Helpers for encoding query in URL
function base64Encode(str) {
    return btoa(encodeURIComponent(str)); // Encode to Base64
}

function base64Decode(str) {
    try {
        return decodeURIComponent(atob(str)); // Decode from Base64
    } catch (e) {
        return ""; // Handle invalid Base64
    }
}

function getQueryParam(name) {
    const params = new URLSearchParams(window.location.search);
    return params.get(name);
}

function setQueryParam(name, value) {
    const params = new URLSearchParams();
    params.set(name, value);
    return `${window.location.origin}${window.location.pathname}?${params.toString()}`;
}

function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        showToast("Link copied to clipboard!");
    }).catch(err => console.error("Failed to copy: ", err));
}

function showToast(message) {
    const toast = document.getElementById("toast");
    toast.textContent = message;
    toast.classList.add("show");

    setTimeout(() => { toast.classList.remove("show"); }, 2000);
}

const defaultQuery = "SELECT *\nFROM aws";

// Text Editor with Syntax Highlighting
let editor;
let rmodule;
document.addEventListener("DOMContentLoaded", async function () {
    const base64Query = getQueryParam("query");
    const initialContent = base64Query ? base64Decode(base64Query) : defaultQuery;
    editor = CodeMirror.fromTextArea(document.getElementById("sql-editor"), {
        mode: "text/x-sql", // SQL mode
        lineNumbers: true, // Show line numbers
        matchBrackets: true, // Highlight matching brackets
        autoCloseBrackets: true // Auto-close brackets
    });
    editor.setValue(initialContent);

    // await import other js file lazily
    try {
        // prepare output area
        let reval = document.getElementById("r-eval");
        // append editor and graphic output area
        reval.innerHTML += '<textarea id="r-editor" class="form-control" rows="10" placeholder="Enter R code here"></textarea>';
        reval.innerHTML += '<button id="execute-r">Plot [Ctrl+Enter]</button>';
        reval.innerHTML += '<div id="r-status" class="output">Loading R...</div>';
        reval.innerHTML += '<div id="r-output" class="output"></div>';
        // load module
        rmodule = await import('./static/plot.js')

        await rmodule.initializeR('r-status')
        console.log("R module loaded successfully");


        let editor = document.getElementById("r-editor");
        let out = document.getElementById("r-output");
        let submit = document.getElementById("execute-r");
        // code mirror editor
        let r_editor = CodeMirror.fromTextArea(editor, {
            mode: "text/x-rsrc", // R mode
            lineNumbers: true, // Show line numbers
            matchBrackets: true, // Highlight matching brackets
            autoCloseBrackets: true, // Auto-close brackets
            extraKeys: {
                "Ctrl-Enter": function (cm) {
                    submit.click();
                }
            }
        });

        // initialize R repl
        await rmodule.makeRRepl(r_editor, out, 'r-output', submit);
        await recreateTable();
        submit.click();
    } catch (error) {
        console.error("Failed to load R module", error);
        alert("Failed to load R module");
    }
});


const MANUAL_BUNDLES = {
    mvp: {
        mainModule: duckdb_wasm,
        mainWorker: mvp_worker,
    },
    eh: {
        mainModule: duckdb_wasm_next,
        mainWorker: eh_worker
    },
};

// Select a bundle based on browser checks
const bundle = await duckdb.selectBundle(MANUAL_BUNDLES);

// Instantiate the asynchronus version of DuckDB-wasm
const worker = new Worker(bundle.mainWorker);
const logger = new duckdb.ConsoleLogger();
const db = new duckdb.AsyncDuckDB(logger, worker);
await db.instantiate(bundle.mainModule, bundle.pthreadWorker);

// Fetch the database file
//TODO: Could fail for large objects
const arrayBuffer = new Uint8Array(await (await fetch(dbfile)).arrayBuffer());

// Register the file in DuckDB's virtual filesystem
await db.registerFileBuffer("cloudspecs.duckdb", new Uint8Array(arrayBuffer));
const conn = await db.connect();
await conn.send("ATTACH 'cloudspecs.duckdb' AS specs;");
await conn.send("USE specs;");

async function createTable() {
    let query = editor.getValue();
    const result = await conn.query(query).then(response => {
        return {
            columns: response.schema.fields.map(field => field.name),
            rows: // Bug fix explained at: https://github.com/GoogleChromeLabs/jsbi/issues/30
            JSON.parse(JSON.stringify(response.toArray(), (key, value) =>
                typeof value === 'bigint' ? parseInt(value.toString()) : value // return everything else unchanged
            ))
        }
    }, error => {
        return { error: error.toString()?.split("\n") }
    });


    if ("error" in result) {
        $("#error-msg").text(result.error);
        return $('#ec2-instances').DataTable({});
    }
    let columns = result.columns.map(key => ({ title: key, data: row => row[key] }));

    // Add column headers to <thead>
    const theadRow = $('#ec2-instances thead tr');
    result.columns.forEach(key => {
        theadRow.append(`<th>${key}</th>`);
    });

    // Set table in R context
    if (rmodule) {
        rmodule.onDataUpdate(result);
    }

    return $('#ec2-instances').DataTable({
        data: result.rows,
        columns: columns,
        ordering: false,
        dom: '<"top-toolbar d-flex justify-content-between align-items-center"iBf>rt<"bottom-toolbar d-flex justify-content-between align-items-center"lp><"clear">',
        buttons: [
            {
                extend: 'csv',
                filename: 'ec2_instances_data',
                text: 'Export Result [CSV]'
            }
            ,
            {
                extend: 'excel',
                filename: 'ec2_instances_data',
                text: 'Export Result [XLS]'
            },
            {
                text: 'Export Database [DuckDB]',
                action: function (e, dt, node, config) {
                    window.location.href = 'https://github.com/TUM-DIS/cloudspecs/blob/main/static/cloudspecs.duckdb'; // Target URL
                },
                className: 'btn btn-primary'  // Bootstrap styling (optional)
            },
            {
                text: 'Share',
                action: function () {
                    const encodedQuery = base64Encode(query);
                    const shareableLink = setQueryParam("query", encodedQuery);
                    copyToClipboard(shareableLink);
                }
            }],
        pageLength: 100, // default row count
        lengthMenu: [25, 50, 100, 200, 1000]
    });
}

// Render initial table with our sample query
let dataTables = await createTable();

const recreateTable = async () => {
    dataTables.clear();
    dataTables.destroy();
    $('#ec2-instances thead').empty().append("<tr></tr>");
    dataTables = await createTable();
}

$(document).ready(async function () {
    $("#load-table").on("click", async function (e) {
        e.preventDefault();
        $("#error-msg").text("");
        recreateTable();
    });

    // $("#tabs").tabs();

    document.addEventListener('keydown', async function (event) {
        if (event.ctrlKey && event.key == "Enter") {
            $("#error-msg").text("");
            recreateTable();
        }
    });
});
