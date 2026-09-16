<template>
  <div class="map-editor">
    <div v-for="(row, index) in rows" :key="row.keyId" class="map-row">
      <el-input v-model="row.name" :placeholder="keyPlaceholder" @input="emitValue" />
      <el-input-number
        v-model="row.value"
        :min="min"
        :max="max"
        :step="step"
        controls-position="right"
        @change="emitValue"
      />
      <el-button text type="danger" @click="remove(index)">移除</el-button>
    </div>
    <el-button text type="primary" @click="add">+ 添加一项</el-button>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: Object, default: () => ({}) },
  keyPlaceholder: { type: String, default: '名称' },
  min: { type: Number, default: 0 },
  max: { type: Number, default: undefined },
  step: { type: Number, default: 1 },
})
const emit = defineEmits(['update:modelValue'])
const rows = ref([])
let syncing = false
let sequence = 0

watch(
  () => props.modelValue,
  value => {
    if (syncing) return
    rows.value = Object.entries(value || {}).map(([name, number]) => ({
      keyId: ++sequence,
      name,
      value: Number(number),
    }))
  },
  { immediate: true, deep: true },
)

function emitValue() {
  const value = {}
  for (const row of rows.value) {
    const name = row.name.trim()
    if (name) value[name] = Number(row.value || 0)
  }
  syncing = true
  emit('update:modelValue', value)
  queueMicrotask(() => { syncing = false })
}

function add() {
  rows.value.push({ keyId: ++sequence, name: '', value: props.min })
}

function remove(index) {
  rows.value.splice(index, 1)
  emitValue()
}
</script>

<style scoped>
.map-editor { display: grid; gap: 8px; width: 100%; }
.map-row { display: grid; grid-template-columns: minmax(120px, 1fr) 130px auto; gap: 8px; align-items: center; }
@media (max-width: 520px) { .map-row { grid-template-columns: 1fr; } }
</style>
