import { defineConfig } from 'vite'

export default defineConfig({
  base: "/cloudspecs/", // for GH pages
  build: {
    target: "esnext", // Needed so that build can occur with the top-level 'await' statements,
  },
})
