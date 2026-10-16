<template>
  <div class="theme-control">
    <label for="theme-choice">主题</label>
    <select id="theme-choice" v-model="theme.theme" aria-label="主题色">
      <option v-for="option in THEME_OPTIONS" :key="option.id" :value="option.id">{{ option.name }}</option>
    </select>
    <button type="button" :aria-label="theme.appearance === 'dark' ? '切换浅色模式' : '切换深色模式'" @click="toggle">
      {{ theme.appearance === 'dark' ? '☼' : '☾' }}
    </button>
  </div>
</template>

<script setup lang="ts">
import { THEME_OPTIONS, useThemeStore } from '@/stores/theme'
const theme = useThemeStore()
function toggle() { theme.setAppearance(theme.appearance === 'dark' ? 'light' : 'dark') }
</script>
