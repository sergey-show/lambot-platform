import "./monacoEnv";
import * as monaco from "monaco-editor";
import "monaco-editor/min/vs/editor/editor.main.css";

const markdownOptions: monaco.editor.IStandaloneEditorConstructionOptions = {
  theme: "vs-dark",
  automaticLayout: true,
  minimap: { enabled: true, scale: 1 },
  fontSize: 13,
  fontFamily: 'Cascadia Code, Consolas, "Courier New", monospace',
  lineNumbers: "on",
  glyphMargin: true,
  scrollBeyondLastLine: false,
  wordWrap: "on",
  tabSize: 2,
  insertSpaces: true,
  detectIndentation: true,
  folding: true,
  foldingStrategy: "indentation",
  renderWhitespace: "selection",
  bracketPairColorization: { enabled: true },
  smoothScrolling: true,
  cursorBlinking: "smooth",
  padding: { top: 8, bottom: 8 },
  scrollbar: {
    verticalScrollbarSize: 10,
    horizontalScrollbarSize: 10,
  },
};

export function mountDocsMarkdownEditor(
  parent: HTMLElement,
  initialValue: string,
): monaco.editor.IStandaloneCodeEditor {
  return monaco.editor.create(parent, {
    ...markdownOptions,
    value: initialValue,
    language: "markdown",
  });
}

export function setDocsMarkdownContent(editor: monaco.editor.IStandaloneCodeEditor, text: string): void {
  editor.setValue(text);
}

export function getDocsMarkdownContent(editor: monaco.editor.IStandaloneCodeEditor): string {
  return editor.getValue();
}
