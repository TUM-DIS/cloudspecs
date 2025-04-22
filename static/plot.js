import { WebR } from 'webr';
import Plotly from 'plotly.js-dist'

// global webR executor
let webR;
let rContext;

const loadRPackages = async () => {
  await webR.installPackages(['ggplot2', 'svglite'], { quiet: true, mount: true });
}

const log = async(msg) => {
  if (!rContext || !('logid' in rContext)) { return;  }
  let log = document.getElementById(rContext.logid);
  log.replaceChildren();
  log.append(msg);
}

export const initializeR = async (logid) => {
  webR = new WebR();
  await webR.init();
  rContext = {logid: logid};
  await loadRPackages();
  await webR.evalRVoid("library(svglite); library(ggplot2)");
  log("R is ready to go!");
}
export const convertBlobToBase64 = blob => new Promise((resolve, reject) => {
    const reader = new FileReader;
    reader.onerror = reject;
    reader.onload = () => {
        resolve(reader.result);
    };
    reader.readAsDataURL(blob);
});

// screen.availHeight**2 + screen.availWidth
const recreatePlot = async () => {
  let {editor, graphic, graphicid} = rContext;
  const code = editor.getValue();
  // console.log(code);
  log("Running code...");
  try {
    const svgstr = await webR.evalRString(code);
    // if is svg -> render svg
    // if is plotly -> render plotly
    // console.log(svgstr);
    // draw plot
    // console.log(JSON.stringify(plotlyData));
    try {
      // graphic.replaceChildren();
      document.getElementById(graphicid).innerHTML = svgstr;

      // Plotly.newPlot(graphicid, JSON.parse(plotlyData), {});
      log("Code ran successfully!");
    } catch (e) {
      console.log(e);
      log("Error drawing plot: " + JSON.stringify(e));
    }
  } catch (e) {
    log("Error running code: " + e);
  }
}

export const onDataUpdate = async (table) => {
  try {
    await webR.objs.globalEnv.bind('df', table.rows);
    console.log('bound new table to R:' );
    await webR.evalR('print(head(df))');
    await recreatePlot();
  } catch (e) {
    log("Error updating data: " + e);
    console.log("failed to re-bind or re-draw data", e);
  }
}

// provide screen size to R environment
export const provideScreenWidth = async(elemid) => {
  try {
    let w = document.getElementById(elemid).clientWidth;
    w = (w == 0  ? window.innerWidth : w) - 20 ;
    let h = window.innerHeight/2;
    await webR.objs.globalEnv.bind('output.width.inch', w/96);
    await webR.objs.globalEnv.bind('output.height.inch', h/96);
    // console.log('bound output size to R:', w, h);
  } catch (e) {
    log("Error updating screen size: " + e);
    console.log("failed to re-bind or re-draw data", e);
  }
}

export const getCurrentEditorContent = () => {
  return rContext.editor.getValue();
}

export const defaultPlot = `to_svg <- svgstring(width = output.width.inch, height = output.height.inch)
theme_set(theme_bw(15))

### the current table is bound to the variable 'df'
output <- ggplot(df, aes(x = release_year, y = vcpu_count, colour = instance_prefix)) +
    geom_point() +
    theme(legend.position = 'none')

## output to the html page
plot(output); dev.off(); to_svg()`;

export const makeRRepl = async (editor, graphic, graphicid, btn, initialContent = null) => {
  const initialPlot = initialContent || defaultPlot;
  editor.getDoc().setValue(initialPlot);
  rContext = {...rContext, editor, graphic, graphicid, btn};
  btn.addEventListener('click', recreatePlot);
  // screen resize listener
  await provideScreenWidth(graphicid);
  window.addEventListener('resize', () => provideScreenWidth(graphicid));
}
