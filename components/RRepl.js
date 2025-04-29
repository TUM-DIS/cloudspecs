import { WebR } from 'webr';

export default class RRepl {
  #webR; #outputSelector;

  constructor(webR, outputSelector) {
    this.#webR = webR;
    this.#outputSelector = outputSelector;
  }

  static async initialize(outputElem) {
    const webR = await this.#initializeWebR();
    const res = new RRepl(webR, outputElem);
    await res.onScreenUpdate();
    return res;
  }

  async eval(rCode, table) {
    try {
      await this.onScreenUpdate();
      await this.onDataUpdate(table);
      return await this.#recreatePlot(rCode);
    } catch (e) {
      return { error: e }
    }
  }

  // private methods

  static async #initializeWebR() {
    const webR = new WebR();
    await webR.init();
    await webR.installPackages(['ggplot2', 'svglite'], { quiet: true, mount: true });
    await webR.evalRVoid("library(svglite); library(ggplot2)");
    return webR;
  }

  async #recreatePlot(code) {
    if (!code) {
      return { error: 'No code provided. Type some R code above!' };
    }
    const svgstr = await this.#webR.evalRString(code);
    $(this.#outputSelector).html(svgstr);
    return { svg: svgstr };
  }

  async onDataUpdate(table) {
    await this.#webR.objs.globalEnv.bind('df', table.rows);
    console.log('bound new table to R:' );
    await this.#webR.evalR('print(head(df))');
  }

  async onScreenUpdate() {
    try {
      let w = document.getElementById(this.#outputSelector).clientWidth;
      w = w == 0  ? 0.9 * window.innerWidth : w;
      let h = window.innerHeight/2;
      await this.#webR.objs.globalEnv.bind('output.width.inch', w/96);
      await this.#webR.objs.globalEnv.bind('output.height.inch', h/96);
      // console.log('bound output size to R:', w, h);
    } catch (e) {
      log("Error updating screen size: " + e);
      console.log("failed to re-bind or re-draw data", e);
    }
  }
}
