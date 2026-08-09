# Halyk AI Workspace — Dashboard Override

This page is an operational product dashboard. The official Halyk challenge website is a
visual-language reference only; its page structure, hero composition and promotional imagery
must not be copied.

## Layout

- Restore the product shell: permanent 248 px sidebar on desktop, compact top bar, dashboard
  overview, agent workspace, decisions table and project documents.
- On widths below 900 px, replace the sidebar with a 72 px mobile header and explicit navigation
  drawer. The page itself must never scroll horizontally.
- Use a 1440 px dashboard canvas with 24–32 px gutters and a 12-column content grid.
- Keep the agent form and run status visible together on wide screens.

## Halyk visual language

| Role | Value |
|---|---|
| Font | `Manrope` |
| Halyk green | `#009B77` |
| Black | `#050706` |
| Page background | `#F5F6F6` |
| Card surface | `#FFFFFF` |
| Muted card | `#F1F2F6` |
| Border | `#E1E4E2` |
| Danger | `#C7433D` |
| Warning | `#8F5A0A` |
| Major radius | `18–20px` |
| Control radius | `10–12px` |

- Use high contrast, generous negative space and restrained green accents.
- Use black as an intentional operational surface, not as a copied marketing hero.
- Cards are flat and border-led; avoid glassmorphism, gradients, large shadows and decorative
  marketing imagery.
- Use one consistent family of simple SVG icons. No emoji icons.

## 21st adaptations

- Use the three-dot processing pattern and thin progress rail only while an agent run is active.
- Use the analytics-bento hierarchy for the overview metrics: a clear primary value, short label
  and small supporting context. Do not reproduce a retrieved component verbatim.

## Product and accessibility

- Public score is explicitly labeled as calibration, never as private-set accuracy.
- All controls have visible labels, 44 px targets, focus states and non-color status text.
- The decision table scrolls inside its own container on small screens.
- Support 375, 768, 1024 and 1440 px and `prefers-reduced-motion`.
