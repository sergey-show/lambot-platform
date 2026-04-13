/**
 * Monaco workers for Vite. Import this module before `import * as monaco from "monaco-editor"`.
 */
import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import jsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import cssWorker from "monaco-editor/esm/vs/language/css/css.worker?worker";
import htmlWorker from "monaco-editor/esm/vs/language/html/html.worker?worker";
import tsWorker from "monaco-editor/esm/vs/language/typescript/ts.worker?worker";

type MonacoWorkerEnv = {
  getWorker: (_moduleId: string, label: string) => Worker;
};

const MonacoEnvironment: MonacoWorkerEnv = {
  getWorker(_moduleId, label) {
    switch (label) {
      case "json":
        return new jsonWorker();
      case "css":
      case "scss":
      case "less":
        return new cssWorker();
      case "html":
      case "handlebars":
      case "razor":
        return new htmlWorker();
      case "typescript":
      case "javascript":
        return new tsWorker();
      default:
        return new editorWorker();
    }
  },
};

(globalThis as unknown as { MonacoEnvironment: MonacoWorkerEnv }).MonacoEnvironment = MonacoEnvironment;
