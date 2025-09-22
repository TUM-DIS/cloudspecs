// For rendering EC2 Table
import './ResultTable.css'
import 'datatables.net-dt/css/dataTables.dataTables.css';
import 'datatables.net-buttons-dt/css/buttons.dataTables.css';

// Import DataTables and plugins
import DataTable from 'datatables.net-dt';
import 'datatables.net-buttons-dt';
import 'datatables.net-buttons/js/buttons.html5.js';
import 'datatables.net-buttons/js/buttons.print.js';
import 'datatables.net-responsive-dt';

import { copyToClipboard } from '/util.js'

// Import export libraries for buttons functionality
import JSZip from 'jszip';
window.JSZip = JSZip;

export default class ResultTable {
  #selector;

  constructor(selector) {
    this.#selector = selector;
  }

  render(columns, rows, query) {
    let elem = $(this.#selector);
    if ( $.fn.dataTable.isDataTable( this.#selector ) ) {
      elem.DataTable().clear();
      elem.DataTable().destroy();
    }
    let mappedCols = columns.map(key => ({ title: key, data: row => row[key] }));
    let tbl = elem.empty().DataTable({
      data: rows,
      columns: mappedCols,
      ordering: false,
      scrollX: true,
      scrollCollapse: true,
      // dom: '<"top-toolbar d-flex justify-content-between align-items-center"lBf>rtip',
      dom: '<"top-toolbar d-flex justify-content-between align-items-center"iBf>rt<"bottom-toolbar d-flex justify-content-between align-items-center"lp><"clear">',
      buttons: [
        {
          extend: 'csv',
          filename: 'cloudspecs_query_result',
          text: 'Export Result [CSV]'
        },
        {
          extend: 'excel',
          filename: 'cloudspecs_query_result',
          text: 'Export Result [XLS]'
        },
        {
          extend: 'print',
          text: 'Print Result'
        }
      ],
      pageLength: 100, // default row count
      lengthMenu: [10, 25, 50, 100, 200, 1000]
    });

    $(tbl.table().header()).on('click', 'th', function() {
      let txt = $(this, '.dt-column-title').text();
      // console.log('copying', txt);
      copyToClipboard(txt, `'${txt}' copied to clipboard`);
    });
  };


};
