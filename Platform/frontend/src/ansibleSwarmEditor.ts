/**
 * Редактор шаблонов Ansible swarm (Superadmin): Monaco (как в VS Code), YAML, тёмная тема.
 */
import "./monacoEnv";
import * as monaco from "monaco-editor";
import "monaco-editor/min/vs/editor/editor.main.css";

const vscodeLikeOptions: monaco.editor.IStandaloneEditorConstructionOptions = {
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

export type AnsibleSwarmEditorPair = {
  meta: monaco.editor.IStandaloneCodeEditor;
  play: monaco.editor.IStandaloneCodeEditor;
  destroy: () => void;
};

function createYamlEditor(parent: HTMLElement, value: string): monaco.editor.IStandaloneCodeEditor {
  return monaco.editor.create(parent, {
    ...vscodeLikeOptions,
    value,
    language: "yaml",
  });
}

export function mountAnsibleSwarmEditors(
  metaParent: HTMLElement,
  playParent: HTMLElement,
  initialMeta: string,
  initialPlay: string,
): AnsibleSwarmEditorPair {
  const meta = createYamlEditor(metaParent, initialMeta);
  const play = createYamlEditor(playParent, initialPlay);
  return {
    meta,
    play,
    destroy: () => {
      meta.dispose();
      play.dispose();
    },
  };
}

export function setEditorContent(editor: monaco.editor.IStandaloneCodeEditor, text: string): void {
  editor.setValue(text);
}

export function getEditorContent(editor: monaco.editor.IStandaloneCodeEditor): string {
  return editor.getValue();
}
