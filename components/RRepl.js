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

  async eval(rCode, table, viewOnly = false) {
    try {
      await this.onScreenUpdate();
      if (!viewOnly) {
        await this.onDataUpdate(table);
      }
      return await this.#recreatePlot(rCode);
    } catch (e) {
      return { error: e }
    }
  }

    static #rPrelude() {
       return `
library(svglite);
library(ggplot2)
colors.area <- c(green = "#a3be8c", "dark-green" = "#469800", purple = "#b48ead", yellow = "#ebcb8b", frost = "#8fbcbb", "dark-blue" = "#5e81ac", "medium-grey" = "#4c566a", red = "#bf616a", "dark-red" = "#ab1b00", orange = "#ff9900", "light-blue" = "#81a1c1", "white-grey" = "#d8dee9", ice = "#88c0d0", carbon = "#2e3440", black = "black")
colors.disc <- c(green = "#8DBE64", "dark-green" = "#469800", purple = "#B470A7", yellow = "#EBB13E", frost = "#6BBCBA", "dark-blue" = "#205FAC", "medium-grey" = "#34466A", red = "#BF1626", "dark-red" = "#AB1B00", orange = "#FF9900", "light-blue" = "#4E87C1", "white-grey" = "#CAD5E9", ice = "#4EB3D0", carbon = "#202A40", black = "#000000")
make.palette <- function(input_mapping, colors_map = colors.disc) {
  # Get the values (colors) from input_mapping and replace with hex values from colors_map
  hex_values <- colors_map[unname(input_mapping)]
  # Return the vector with column names mapped to hex values
  output_vector <- setNames(hex_values, names(input_mapping))
  return(output_vector)
}
colormap.vendor <- c('Unknown' = 'grey', 'AMD' = 'carbon', 'Intel' = 'dark-blue', 'Graviton' = 'orange', 'AWS Graviton' = 'orange')
palette.vendor <- make.palette(colormap.vendor)
options(ggplot2.discrete.colour= unname(colors.disc[c("dark-green", "dark-blue", "dark-red", "purple", "yellow", "carbon", "orange", "light-blue", "ice")]))
` 
    }

  minimalRCode() {
    return `to_svg <- svgstring(width = output.width.inch, height = output.height.inch, scaling = 1)
theme_set(theme_bw())

### the current table is bound to the variable 'df'
output <- ggplot(df, aes()) +
  annotate(geom = 'text', x = 0, y = 0, label = 'Plot something!')

## output to the html page
plot(output); dev.off(); to_svg()`
  }

  // private methods

  static async #initializeWebR() {
    const webR = new WebR();
    await webR.init();
    await webR.installPackages(['ggplot2', 'svglite'], { quiet: true, mount: true });
    await webR.evalRVoid(this.#rPrelude());
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
