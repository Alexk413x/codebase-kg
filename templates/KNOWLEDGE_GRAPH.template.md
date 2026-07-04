# <Codebase> — Knowledge Graph

```
codebase:    <name>                 # e.g. android | ios | web | backend
root:        <path to the code root, repo-relative>
counterpart: <../other-repo/KNOWLEDGE_GRAPH.md>   # optional — only for a paired codebase
language:    <hint, optional>       # e.g. kotlin | swift | typescript (for symbol tooling)
refreshed:   <YYYY-MM-DD>
```

Born <date>. <One line: what this doc is and how to use it as a search index.>

> **Update policy.** Every KG update must **fully** reflect the change in the **node tables**,
> not just the header. For each change: **add** nodes for new files/features, **edit** the nodes
> for changed files (anchors, summary, edges, parity), **remove/rename** nodes for deleted/renamed
> files, and refresh the affected `## EDGES` flows. **A header-or-date-only edit is forbidden** —
> that habit causes node drift. When you add a top-level component, add a node and an edge to its
> dependencies. Bump `refreshed:` to today.

---

## NODES

### <SECTION — e.g. APP ENTRY>

<!-- updated = date this node was last verified vs source. parity / counterpart /
     divergence are optional, multi-codebase only: omit counterpart when parity is
     <codebase>-only; give divergence only when parity = divergent. Do NOT put
     inline `# comments` inside table rows — they become part of the value. -->

| id          | <stable-slug> |
| kind        | <free-text role: Composable / ViewModel / Service / module / actor / @Model / …> |
| anchors     | `<path#Symbol>`, `<path#OtherSymbol>` |
| summary     | <what it is/does — this codebase only; pointer-dense; no copied code> |
| edges       | <other-node-id>, <other-node-id> |
| updated     | <YYYY-MM-DD> |
| parity      | <matched | divergent | <codebase>-only> |
| counterpart | <../other-repo/KNOWLEDGE_GRAPH.md#node-id> |
| divergence  | <one short line> |

<!-- repeat the node block per component; add ### sections that fit this codebase -->

## EDGES

### <FLOW NAME — e.g. DATA FLOW>
- <node> → <node> → <node>

## FEATURE → CODE MAP

<!-- optional wide-table search index: feature → primary anchors -->
| Feature | Primary anchors |
|---|---|
| <feature> | `<path#Symbol>`, `<path#Symbol>` |

## PARITY GAPS

<!-- optional, multi-codebase only — a POINTER, not a copy. Run kg_parity_gaps for the live list. -->

## KEY DECISIONS

<!-- optional: durable architectural constraints -->

## ARCHITECTURE SUMMARY

<!-- optional: one or two paragraphs for cold-start orientation -->
