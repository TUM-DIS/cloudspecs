/* Import npm package styles */
import 'jquery-ui/dist/themes/ui-lightness/jquery-ui.css';

// Import jQuery and make it global for compatibility
import $ from 'jquery';
window.jQuery = window.$ = $;

// Import jQuery UI after jQuery is set up (in main.js)
// to ensure correct load order
