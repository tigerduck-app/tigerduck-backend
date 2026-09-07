// Vite's ambient declarations for the asset imports (`./index.css`) and the
// `import.meta.env` shape. TypeScript 7 rejects a side-effect import of a
// module it has no declaration for, so this reference is load-bearing rather
// than cosmetic — without it `tsc -b` fails on main.tsx.
/// <reference types="vite/client" />
