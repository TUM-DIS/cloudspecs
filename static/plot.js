import { WebR } from 'webr';
import Plotly from 'plotly.js-dist'

// global webR executor
let webR;
let rContext;

const loadRPackages = async () => {
  await webR.installPackages(['jsonlite', 'ggplot2', 'plotly'], true);
}

const log = async(msg) => {
    let log = document.getElementById(rContext.logid);
    log.replaceChildren();
    log.append(msg);
}

export const initializeR = async (logid) => {
  webR = new WebR();
  await webR.init();
  rContext = {logid: logid};
  await loadRPackages();
  log("R is ready to go!");
}

const recreatePlot = async () => {
  let {editor, graphic, graphicid} = rContext;
  const code = editor.getValue();
  log("Running code...");
  try {
    const plotlyData = await webR.evalRString(code);
    // draw plot
    // console.log(JSON.stringify(plotlyData));
    try {
      graphic.replaceChildren();
      Plotly.newPlot(graphicid, JSON.parse(plotlyData), {});
      log("Code ran successfully!");
    } catch (e) {
      console.log(e);
      log("Error drawing plot: " + JSON.stringify(plotlyData));
    }
  } catch (e) {
    log("Error running code: " + e);
  }
}

export const onDataUpdate = async (table) => {
  await webR.objs.globalEnv.bind('df', table.rows);
  console.log('bound new table to R:' );
  await webR.evalR('print(head(df))');
  await recreatePlot();
}

export const makeRRepl = (editor, graphic, graphicid, btn) => {
  editor.getDoc().setValue(`### the table above is bound to the variable 'df'
library(plotly)
library(ggplot2)
theme_set(theme_bw(15))

df[['family']] <- sapply(strsplit(df[['Name']], '\\\\.'), function(x) x[1])
p <- ggplot(df, aes(x = .data[['SPEC..']], y = On.Demand.Price, colour = family)) +
  expand_limits(y = 0, x = 0) +
  geom_point()

plotly_json(p, pretty = FALSE)`)
  rContext = {...rContext, editor, graphic, graphicid, btn};
  btn.addEventListener('click', recreatePlot);
}
