// デザイントークン。色、余白、角丸、文字サイズはここだけで決める。
// コンポーネントは Mantine のテーマ値(var(--mantine-*))と、下の --cc-*(意味で名付けた色)だけを使う。

import { createTheme, type CSSVariablesResolver, type MantineColorsTuple } from '@mantine/core';

// 主色: 落ち着いた青。映像の邪魔をしない彩度に抑える
const brand: MantineColorsTuple = [
  '#e8f3ff', '#d2e3fb', '#a4c4f2', '#73a4ea', '#4b88e3', '#3277df', '#226ede', '#135dc6', '#0552b2', '#00469e',
];

export const theme = createTheme({
  primaryColor: 'brand',
  primaryShade: { light: 7, dark: 5 },
  colors: { brand },
  fontFamily:
    'system-ui, -apple-system, "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Noto Sans JP", "Yu Gothic UI", Meiryo, sans-serif',
  fontFamilyMonospace: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  defaultRadius: 'md',
  radius: { xs: '2px', sm: '4px', md: '8px', lg: '12px', xl: '16px' },
  spacing: { xs: '6px', sm: '10px', md: '16px', lg: '24px', xl: '32px' },
  fontSizes: { xs: '12px', sm: '13px', md: '14px', lg: '16px', xl: '20px' },
  lineHeights: { xs: '1.4', sm: '1.45', md: '1.55', lg: '1.6', xl: '1.65' },
  headings: {
    fontWeight: '600',
    sizes: {
      h1: { fontSize: '20px', lineHeight: '1.3' },
      h2: { fontSize: '16px', lineHeight: '1.35' },
      h3: { fontSize: '14px', lineHeight: '1.4' },
    },
  },
  cursorType: 'pointer',
  components: {
    Badge: { defaultProps: { radius: 'sm', variant: 'light' } },
    Tooltip: { defaultProps: { openDelay: 300, withArrow: true } },
    ActionIcon: { defaultProps: { variant: 'subtle', color: 'gray' } },
  },
});

// 意味で名付けた色(シークバー、状態表示、映像の周り)。ライトとダークで別の値を持つ。
export const cssVariablesResolver: CSSVariablesResolver = (t) => ({
  variables: {
    '--cc-header-height': '52px',
    '--cc-video-bg': '#000',
    '--cc-video-overlay': 'rgba(0, 0, 0, 0.55)',
    '--cc-video-overlay-text': '#fff',
    // PTZ のミニマップ(黒い映像の上に描くので、ライト/ダーク共通)
    '--cc-ptz-window': t.colors.brand[4],
    '--cc-ptz-window-fill': 'rgba(75, 136, 227, 0.25)',
  },
  light: {
    '--cc-surface': t.white,
    '--cc-surface-muted': t.colors.gray[0],
    '--cc-border': t.colors.gray[3],
    '--cc-track': t.colors.gray[2],
    '--cc-track-future': `repeating-linear-gradient(45deg, ${t.colors.gray[1]} 0 4px, ${t.colors.gray[2]} 4px 8px)`,
    '--cc-rec-s3': t.colors.brand[6],
    '--cc-rec-kvs': t.colors.teal[5],
    '--cc-rec-failed': t.colors.red[6],
    '--cc-playhead': t.colors.dark[7],
    '--cc-selected': t.colors.brand[0],
  },
  dark: {
    '--cc-surface': t.colors.dark[7],
    '--cc-surface-muted': t.colors.dark[8],
    '--cc-border': t.colors.dark[4],
    '--cc-track': t.colors.dark[5],
    '--cc-track-future': `repeating-linear-gradient(45deg, ${t.colors.dark[6]} 0 4px, ${t.colors.dark[5]} 4px 8px)`,
    '--cc-rec-s3': t.colors.brand[4],
    '--cc-rec-kvs': t.colors.teal[4],
    '--cc-rec-failed': t.colors.red[5],
    '--cc-playhead': t.colors.gray[0],
    '--cc-selected': 'rgba(50, 119, 223, 0.18)',
  },
});
