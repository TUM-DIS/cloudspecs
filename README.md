# Cloudspecs: Cloud Hardware Evolution Through the Looking Glass

| URL | Branch | Description 
|-|- |-
| <https://cloudspecs.fyi> | `main` | Including latest instance types
| <https://tum-dis.github.io/cloudspecs> | `cidr2026` | Frozen database state for reproducibility of our CIDR 2026 paper 

## Paper

Cloudspecs was accepted at the [CIDR 2026](https://www.cidrdb.org/cidr2026/index.html) conference.
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
