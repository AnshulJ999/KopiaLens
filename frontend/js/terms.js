// The app's named terms: one label, one tooltip and one glossary line each. Column headers, card
// labels and help tooltips read from here, so renaming a term is one edit. Sentences use plain
// words ("frees") and are not swapped.
const TERMS = {
  reclaimable: {
    label: "Reclaimable",
    tip: "Space you get back by deleting this. It comes back after the next full maintenance.",
    glossary: "The storage a delete gives back: data that nothing kept still uses. Kopia releases it during full maintenance.",
  },
  reclaimableOwn: {
    label: "Reclaimable on its own",
    tip: "Space only this uses. Pieces it shares with other files stay.",
    glossary: "What deleting just this item gives back. Data it shares with other files stays, because they still need it.",
  },
  reclaimableAll: {
    label: "Reclaimable (all versions)",
    tip: "Space you get back by removing every version of this file from every snapshot.",
    glossary: "What removing a file from all of its snapshots gives back, across every stored version.",
  },
};
const term = (key) => (TERMS[key] || {}).label || key;
const termTip = (key) => (TERMS[key] || {}).tip || "";
