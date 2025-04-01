import './style.css';

import * as duckdb from '@duckdb/duckdb-wasm';
import duckdb_wasm from '@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm?url';
import mvp_worker from '@duckdb/duckdb-wasm/dist/duckdb-browser-mvp.worker.js?url';
import duckdb_wasm_next from '@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url';
import eh_worker from '@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?url';

// For rendering EC2 Table
import 'datatables.net-responsive-dt';

// Load static DuckDB database from GitHub
import dbfile from './static/benchmark.duckdb?url';

// Text Editor with Syntax Highlighting
let editor;
let rmodule;
document.addEventListener("DOMContentLoaded", async function () {
    editor = CodeMirror.fromTextArea(document.getElementById("sql-editor"), {
        mode: "text/x-sql", // SQL mode
        lineNumbers: true, // Show line numbers
        matchBrackets: true, // Highlight matching brackets
        autoCloseBrackets: true // Auto-close brackets
    });

    // await import other js file lazily
    try {
        // prepare output area
        let reval = document.getElementById("r-eval");
        // append editor and graphic output area
        reval.innerHTML += '<textarea id="r-editor" class="form-control" rows="10" placeholder="Enter R code here"></textarea>';
        reval.innerHTML += '<button id="execute-r">Run</button>';
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
await db.registerFileBuffer("benchmark.duckdb", new Uint8Array(arrayBuffer));
const conn = await db.connect();
await conn.send("ATTACH 'benchmark.duckdb' AS bench;");

async function createTable() {
    let query = editor.getValue();
    const result = await conn.query(query).then(response => {
        return {
            columns: response.schema.fields.map(field => field.name),
            rows: // Bug fix explained at: https://github.com/GoogleChromeLabs/jsbi/issues/30
                JSON.parse(JSON.stringify(response.toArray(), (key, value) =>
                    typeof value === 'bigint' ? value.toString() : value // return everything else unchanged
                ))
        }
    },
        error => {
            return { error: error.toString()?.split("\n") }
        });


    if ("error" in result) {
        $("#error-msg").text(result.error);
        return $('#ec2-instances').DataTable({});
    }
    else {
        let columns = result.columns.map(key => ({ title: key, data: row => row[key] }));

        // Add column headers to <thead>
        const theadRow = $('#ec2-instances thead tr');
        result.columns.forEach(key => {
            theadRow.append(`<th>${key}</th>`);
        });

        // Set table in R context
        try {
            if (rmodule) {
                console.log("Setting table in R context", result);
                rmodule.onDataUpdate(result);
            }
        } catch (error) {
            console.error("Failed to set table in R context", error);
        }

        return $('#ec2-instances').DataTable({
            data: result.rows,
            columns: columns,
            ordering: false,
            dom: '<"top-toolbar d-flex justify-content-between align-items-center"lBf>rtip',
            buttons: [
                {
                    extend: 'csv',
                    filename: 'ec2_instances_data',
                    text: 'CSV'
                }
                ,
                {
                    extend: 'excel',
                    filename: 'ec2_instances_data',
                    text: 'Excel'
                },
                // {
                //     text: 'GGPlot',
                //     className: 'btn btn-primary',
                //     action: function (e, dt, node, config) {
                //         $('#r-eval').toggle();
                //     }
                // },
                {
                    text: 'DuckDB [Whole Database]',
                    action: function (e, dt, node, config) {
                        window.location.href = 'https://github.com/TUM-DIS/EC2Bench/blob/main/static/benchmark.duckdb'; // Target URL
                    },
                    className: 'btn btn-primary'  // Bootstrap styling (optional)
                }]
        });
    }

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

    $("#tabs").tabs();

    document.addEventListener('keydown', async function (event) {
        if (event.ctrlKey && event.key == "Enter") {
            //e.preventDefault();
            $("#error-msg").text("");
            recreateTable();
        }
    });
});
