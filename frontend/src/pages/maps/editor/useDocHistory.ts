// The editor's working copy of the document with undo and redo. Whole documents are kept
// (they are small and never mutated), so a step is just a reference.

import { useCallback, useState } from "react";
import type { MapDocument } from "../../../api";

const LIMIT = 100;

interface State {
  doc: MapDocument;
  past: MapDocument[];
  future: MapDocument[];
  /** Edits with the same key in a row (typing in one field) are one undo step. */
  key: string | null;
}

export function useDocHistory(initial: MapDocument) {
  const [s, setS] = useState<State>({ doc: initial, past: [], future: [], key: null });

  /** One undo step. */
  const commit = useCallback((next: MapDocument, key: string | null = null) => {
    setS((c) => {
      if (next === c.doc) return c;
      if (key !== null && key === c.key) return { ...c, doc: next, future: [] };
      return { doc: next, past: [...c.past, c.doc].slice(-LIMIT), future: [], key };
    });
  }, []);

  /** Show a change while dragging, without a step; `commitFrom` makes it one. */
  const preview = useCallback((next: MapDocument) => setS((c) => ({ ...c, doc: next })), []);

  /** The drag is over: `base` (the document before it) becomes the undo step. */
  const commitFrom = useCallback((base: MapDocument) => {
    setS((c) =>
      c.doc === base
        ? c
        : { doc: c.doc, past: [...c.past, base].slice(-LIMIT), future: [], key: null },
    );
  }, []);

  const undo = useCallback(() => {
    setS((c) => {
      const prev = c.past[c.past.length - 1];
      if (!prev) return c;
      return { doc: prev, past: c.past.slice(0, -1), future: [c.doc, ...c.future], key: null };
    });
  }, []);

  const redo = useCallback(() => {
    setS((c) => {
      const next = c.future[0];
      if (!next) return c;
      return { doc: next, past: [...c.past, c.doc], future: c.future.slice(1), key: null };
    });
  }, []);

  return {
    doc: s.doc,
    /** Changed since the editor opened (an undo back to the start is not a change). */
    dirty: s.doc !== initial,
    canUndo: s.past.length > 0,
    canRedo: s.future.length > 0,
    commit,
    preview,
    commitFrom,
    undo,
    redo,
  };
}
