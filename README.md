# Cloudspecs: Cloud Hardware Evolution Through the Looking Glass

| URL | Branch | Description 
|-|- |-
| <https://cloudspecs.fyi> | `main` | Including latest instance types
| <https://tum-dis.github.io/cloudspecs> | `cidr2026` | Frozen database state for reproducibility of our CIDR 2026 paper 

## Paper

Cloudspecs was accepted at the [CIDR 2026](https://www.cidrdb.org/cidr2026/papers.html) conference.
A preprint of our paper will be available soon.
If you find Cloudspecs or our analysis useful for your research, please consider citing:
```
@inproceedings{DBLP:conf/cidr/SteinertKL26,
  author       = {Till Steinert and
                  Maximilian Kuschewski and
                  Viktor Leis},
  title        = {Cloudspecs: Cloud Hardware Evolution Through the Looking Glass},
  booktitle    = {{CIDR}},
  publisher    = {www.cidrdb.org},
  year         = {2026}
}
```

## Installation
You can run also run Cloudspecs locally by cloning this repository.
We use [npm](https://www.npmjs.com/) for package management and [vite](https://vite.dev/) for serving the website.<br>
Install required packages:
```bash
npm i
```
Start a development server:
```bash
npm run dev
```
You can access your local deployment at [`http://localhost:5173/`](http://localhost:5173/)

## Custom Reproducability Sites
You can use the Cloudspecs framework to create custom reproducibility sites for your own papers by forking this repository and replacing Cloudspecs with your own database.
You'll probably want to replace/adapt the following files:
- static/cloudspecs.duckdb
- static/sample-queries.json
- components/db.js
- vite.config.js

Cloudspecs does not require a web server and can be hosted e.g., on GitHub Pages.
We also provide a LaTex package (in the `resource` folder) that showcases creating clickable figures that reference your reproducability site from within your paper.
