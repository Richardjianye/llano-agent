/**
 * plugins/vuetify.ts
 *
 * Framework documentation: https://vuetifyjs.com
 */

import { createVuetify } from 'vuetify'
import '@mdi/font/css/materialdesignicons.css'
import 'vuetify/styles'

/**
 * 表面色阶（surface elevation scale）
 *
 * 之前 background / surface / surface-light 三者只差 3~5 个灰度值，
 * 卡片、侧边栏、顶栏叠在页面底色上完全看不出边界，整屏糊成一块灰。
 * 现在按「画布 → 容器 → 强调」拉开三档，并让 surface 明确亮于 background：
 *
 *   surface-dim    最暗一档，表头 / 分组底、hover 之外的次级区域
 *   background     页面画布（v-main 的底）
 *   surface-light  次级容器：数据表表头、代码块、被选中行
 *   surface        主容器：卡片 / 侧边栏 / 顶栏 / 弹窗
 *   surface-bright 最亮一档，需要跳出来的浮层
 *
 * 深色主题原来 surface(#161616) 比 background(#2f2f2f) 还暗，卡片看着像挖了个洞，
 * 属于层级倒挂，一并修正为「容器亮于画布」。
 */
const lightSurfaces = {
  'background': '#e8eaea',
  'surface': '#fcfdfd',
  'surface-bright': '#ffffff',
  'surface-light': '#f1f3f3',
  'surface-dim': '#d8dbdb',
  'on-surface': '#1a1c1c',
}

const darkSurfaces = {
  'background': '#121414',
  'surface': '#1d2020',
  'surface-bright': '#2e3232',
  'surface-light': '#262a2a',
  'surface-dim': '#0d0f0f',
  'on-surface': '#e0e3e2',
}

export default createVuetify({
  theme: {
    defaultTheme: 'system',
    // ⚠️ 不要设成 false。组件的 color 属性并不直接写行内样式，而是加一个
    // `bg-primary` / `text-primary` 类名（见 vuetify/lib/composables/color.js），
    // 这些类的 CSS 正是由 utilities 开关生成的。关掉之后类名照加、样式没有，
    // 于是所有 color="primary|success|warning" 全部失效，页面变成一片灰。
    //
    // Tailwind 的 @theme 虽然也能生成同名类，但它靠扫描源码，
    // 而这些类是 Vuetify 运行时才拼出来的，扫不到；
    // 且 Tailwind 版只设 background-color，丢掉了 Vuetify 配套的
    // `color: on-primary` 前景色，按钮文字会看不清。两个理由都要求这里为 true。
    utilities: true,
    themes: {
      light: {
        colors: {
          ...lightSurfaces,
          'outline': '#6b6f6e',
          'outline-variant': '#c3c8c7',
          'primary': '#0f6b4b',
          'on-primary': '#ffffff',
          'secondary': '#3d6626',
          'on-secondary': '#ffffff',
          'tertiary': '#1a6169',
          'on-tertiary': '#ffffff',
          'error': '#ba1a1a',
          'on-error': '#ffffff',
          'success': '#1b7a4b',
          'on-success': '#ffffff',
          'warning': '#8a5300',
          'on-warning': '#ffffff',
          'info': '#1a6169',
          'on-info': '#ffffff',
        },
        dark: false,
        variables: {
          'overlay-background': '#1a1c1c',
          // 默认 0.12 的黑色描边压在浅灰底上几乎看不见，卡片的 border 形同虚设。
          'border-color': '#0f1211',
          'border-opacity': 0.16,
          'shadow-color': '#0f1211',
          'theme-code': '#eceeee',
          'theme-on-code': '#1a1c1c',
          'theme-kbd': '#e0e3e2',
          'theme-on-kbd': '#1a1c1c',
        },
      },
      dark: {
        colors: {
          ...darkSurfaces,
          'outline': '#8b918f',
          'outline-variant': '#3c4241',
          'primary': '#72d6a9',
          'on-primary': '#06382a',
          'secondary': '#9fd07a',
          'on-secondary': '#243c19',
          'tertiary': '#67cfd4',
          'on-tertiary': '#123b3f',
          'error': '#ffb4ab',
          'on-error': '#690005',
          'success': '#7ddba4',
          'on-success': '#06381e',
          'warning': '#f7c26b',
          'on-warning': '#3d2a00',
          'info': '#67cfd4',
          'on-info': '#123b3f',
        },
        dark: true,
        variables: {
          'overlay-background': '#000000',
          'border-color': '#e0e3e2',
          'border-opacity': 0.16,
          'shadow-color': '#000000',
          'theme-code': '#262a2a',
          'theme-on-code': '#cdd2d1',
          'theme-kbd': '#2e3232',
          'theme-on-kbd': '#e0e3e2',
        },
      },
    },
  },

  /**
   * 组件默认值
   *
   * 目的是把「卡片长什么样」这件事收敛到一处：页面里散落的
   * `border flat rounded="lg"` 之前每个卡片都要重写一遍，弹窗里的
   * `<v-card :title="...">` 又漏写，于是同一屏出现两种卡片样式。
   * 这里定好基线后，页面只在需要偏离基线时才写 prop。
   */
  defaults: {
    VCard: {
      border: true,
      flat: true,
      rounded: 'lg',
    },
    VNavigationDrawer: {
      color: 'surface',
      border: 'e',
    },
    VAppBar: {
      color: 'surface',
      flat: true,
      border: 'b',
    },
    VToolbar: {
      color: 'surface',
    },
    VDataTable: {
      density: 'comfortable',
      hover: true,
    },
    VDataTableServer: {
      density: 'comfortable',
      hover: true,
    },
    VTextField: {
      variant: 'outlined',
      density: 'comfortable',
    },
    VTextarea: {
      variant: 'outlined',
      density: 'comfortable',
    },
    VSelect: {
      variant: 'outlined',
      density: 'comfortable',
    },
    VAutocomplete: {
      variant: 'outlined',
      density: 'comfortable',
    },
    VCombobox: {
      variant: 'outlined',
      density: 'comfortable',
    },
    VDialog: {
      // 弹窗里的卡片不描边、改用阴影浮起来。注意 .v-card--border 会把
      // box-shadow 清成 none，所以 border 和 elevation 只能二选一。
      VCard: {
        border: false,
        elevation: 8,
        flat: false,
        rounded: 'lg',
      },
    },
    VMenu: {
      VList: { density: 'comfortable' },
    },
  },

  display: {
    mobileBreakpoint: 'md',
    thresholds: {
      xs: 0,
      sm: 600,
      md: 840,
      lg: 1145,
      xl: 1545,
      xxl: 2138,
    },
  },
})
