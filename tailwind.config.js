// Tailwind CSS 配置 — 生产构建（替代 CDN 运行时编译）
//
// 与原 base.html 内联 tailwind.config 等价，保留 darkMode: 'media'
// 以与 tokens.css 的 @media (prefers-color-scheme: dark) 一致。
// 构建链路见 Dockerfile (css-builder stage) + scripts/build-css.sh。
module.exports = {
  darkMode: 'media',
  content: [
    // 全部 HTML 模板（含 macros / partials / 未来新增的平台模板）
    './web/templates/**/*.html',
    // 防御性扫描 Python 路由里硬编码的 HTML 片段（如果有）
    './web/routes/**/*.py',
  ],
  theme: {
    extend: {
      colors: {
        apple: {
          blue: '#0071e3',
          'blue-dark': '#0a84ff',
          green: '#34c759',
          'green-dark': '#30d158',
          orange: '#ff9500',
          'orange-dark': '#ff9f0a',
          red: '#ff3b30',
          'red-dark': '#ff453a',
        },
      },
      boxShadow: {
        stat: 'var(--shadow-stat)',
        card: 'var(--shadow-card)',
        tooltip: 'var(--shadow-tooltip)',
      },
    },
  },
};
