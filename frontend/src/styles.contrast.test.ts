import { describe, expect, it } from 'vitest'

import styles from './styles.css?raw'

// 设计 token 的对比度门禁：小字号标签（10.5–12px）必须达到 WCAG AA 的 4.5:1。
// 值直接解析自 styles.css，避免测试与样式各写一份色值。

function hex(name: string): string {
  const match = new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})\\s*;`).exec(styles)
  if (!match) throw new Error(`styles.css 里找不到 --${name}`)
  return match[1]
}

function rgb(value: string): [number, number, number] {
  return [1, 3, 5].map((offset) => Number.parseInt(value.slice(offset, offset + 2), 16)) as
    [number, number, number]
}

function luminance([red, green, blue]: [number, number, number]): number {
  const channel = (value: number) => {
    const ratio = value / 255
    return ratio <= 0.04045 ? ratio / 12.92 : ((ratio + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)
}

function contrast(foreground: string, background: string): number {
  const first = luminance(rgb(foreground))
  const second = luminance(rgb(background))
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05)
}

describe('浅色主题文字对比度', () => {
  const surfaces = ['bg-app', 'bg-shell', 'surface', 'surface-sunken', 'surface-solid']

  it('正文、次要文字与辅助标签在所有中性表面上满足 4.5:1', () => {
    for (const foreground of ['text', 'text-2', 'text-3']) {
      for (const background of surfaces) {
        expect(contrast(hex(foreground), hex(background)), `${foreground} / ${background}`)
          .toBeGreaterThanOrEqual(4.5)
      }
    }
    expect(luminance(rgb(hex('text')))).toBeLessThan(luminance(rgb(hex('text-2'))))
    expect(luminance(rgb(hex('text-2')))).toBeLessThan(luminance(rgb(hex('text-3'))))
  })

  it('主按钮与链接在默认和悬停状态下满足 4.5:1', () => {
    for (const background of ['accent', 'accent-deep']) {
      expect(contrast(hex('text-on-accent'), hex(background))).toBeGreaterThanOrEqual(4.5)
    }
    for (const background of surfaces) {
      expect(contrast(hex('accent-soft'), hex(background))).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('警告、错误和成功文字满足 4.5:1', () => {
    expect(contrast(hex('warning'), hex('warning-surface'))).toBeGreaterThanOrEqual(4.5)
    expect(contrast(hex('danger'), hex('danger-surface'))).toBeGreaterThanOrEqual(4.5)
    expect(contrast(hex('ok'), hex('surface'))).toBeGreaterThanOrEqual(4.5)
  })
})
