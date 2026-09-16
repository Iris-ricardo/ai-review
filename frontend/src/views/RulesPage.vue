<template>
  <div class="rule-studio">
    <header class="studio-header">
      <div>
        <h1>规则标准管理台</h1>
        <p>结构化配置、样例验证、版本审批和安全发布</p>
      </div>
      <div class="header-actions">
        <el-tag v-if="currentUser" type="success">{{ currentUser.display_name || currentUser.username }} · {{ currentUser.role_label }}</el-tag>
        <el-input v-else v-model="adminToken" type="password" show-password placeholder="兼容管理员凭据" @change="authenticate" />
        <el-button type="primary" :disabled="!can('ruleset.edit')" @click="newDialog = true">新建规则集</el-button>
      </div>
    </header>

    <div class="studio-grid" :class="{ 'has-property': tab === 'rules' }">
      <aside class="ruleset-nav panel">
        <el-input v-model="search" clearable placeholder="搜索规则集" />
        <div class="nav-create">
          <div>
            <strong>规则集</strong>
            <span>新建、停用、删除草稿都从这里开始</span>
          </div>
          <el-button size="small" type="primary" :disabled="!can('ruleset.edit')" @click="newDialog = true">新建</el-button>
        </div>
        <el-alert
          class="nav-tip"
          title="发布过的规则集保留历史，只能停用；未发布草稿可以删除。"
          type="info"
          :closable="false"
          show-icon
        />
        <div class="nav-list">
          <button
            v-for="item in filteredRulesets" :key="item.id"
            class="nav-item" :class="{ active: item.id === selectedId }"
            @click="loadRuleset(item.id)"
          >
            <span class="nav-name">{{ item.name || item.id }}</span>
            <span class="nav-meta">
              <el-tag size="small" :type="item.status === 'disabled' ? 'danger' : item.working_version ? 'warning' : 'success'">
                {{ statusText(item) }}
              </el-tag>
              {{ item.rule_count }} 条
            </span>
          </button>
        </div>
      </aside>

      <main class="rule-workspace panel" v-loading="loading">
        <template v-if="detail">
          <div class="workspace-title">
            <div>
              <el-input v-model="model.name" class="name-input" :disabled="readOnly" />
              <div class="muted">{{ detail.id }} · {{ versionLabel }}</div>
            </div>
            <div class="toolbar">
              <el-button v-if="!working && detail.active_version" :disabled="!can('ruleset.edit')" @click="createDraft">创建编辑草稿</el-button>
              <el-button v-if="!readOnly" :disabled="!can('ruleset.edit')" @click="validateCurrent">校验</el-button>
              <el-button v-if="!readOnly" type="primary" :disabled="!can('ruleset.edit')" :loading="saving" @click="saveDraft">保存草稿</el-button>
              <el-dropdown @command="lifecycleAction">
                <el-button>发布管理⌄</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item v-if="working?.state === 'draft'" :disabled="!can('ruleset.submit')" command="submit">提交审核</el-dropdown-item>
                    <el-dropdown-item v-if="working?.state === 'submitted'" :disabled="!can('ruleset.approve') || !isAssignedReviewer" command="approve">审核通过</el-dropdown-item>
                    <el-dropdown-item v-if="working?.state === 'submitted'" :disabled="!can('ruleset.approve') || !isAssignedReviewer" command="reject">驳回整改</el-dropdown-item>
                    <el-dropdown-item v-if="working?.state === 'approved'" :disabled="!can('ruleset.publish') || !isAssignedReviewer" command="publish">发布已审核版本</el-dropdown-item>
                    <el-dropdown-item :disabled="!can('ruleset.disable')" command="toggle-status">{{ detail.status === 'disabled' ? '恢复规则集' : '停用规则集' }}</el-dropdown-item>
                    <el-dropdown-item v-if="canDiscardDraft" :disabled="!can('ruleset.delete_draft')" divided command="delete-draft">删除当前草稿</el-dropdown-item>
                    <el-dropdown-item v-if="canDeleteRuleset" :disabled="!can('ruleset.delete')" divided command="delete-ruleset">删除规则集</el-dropdown-item>
                    <el-dropdown-item v-else-if="detail.active_version" disabled divided>发布后仅可停用</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </div>

          <div v-if="working" class="workflow-panel">
            <el-tag :type="workflowTagType(working.state)">{{ workflowStateLabel(working.state) }}</el-tag>
            <el-tag :type="working.latest_test?.status === 'done' ? 'success' : 'danger'">
              {{ working.latest_test?.status === 'done' ? '当前版本已通过样例测试' : '当前版本尚未通过样例测试' }}
            </el-tag>
            <span>创建：{{ working.created_by || '-' }}</span>
            <span v-if="working.submitted_by">提交：{{ working.submitted_by }}</span>
            <span v-if="working.assigned_reviewer">指定审核：{{ working.assigned_reviewer }}</span>
            <span v-if="working.approved_by">审核通过：{{ working.approved_by }}</span>
            <span v-if="working.rejection_reason" class="rejection">驳回原因：{{ working.rejection_reason }}</span>
          </div>

          <el-tabs v-model="tab">
            <el-tab-pane label="规则配置" name="rules">
              <div class="rule-toolbar">
                <el-input v-model="ruleSearch" clearable placeholder="搜索规则名称、ID 或类型" />
                <el-button :disabled="readOnly || !can('ruleset.edit')" @click="addRule">添加规则</el-button>
              </div>
              <el-empty v-if="!visibleRules.length" description="暂无规则" />
              <article
                v-for="(rule, index) in visibleRules" :key="rule.id"
                class="rule-card" :class="{ selected: selectedRule?.id === rule.id, disabled: !rule.enabled }"
                @click="selectRule(rule)"
              >
                <div class="rule-index">{{ index + 1 }}</div>
                <div class="rule-main">
                  <div class="rule-title">{{ rule.description || checkerMap[rule.type]?.label || rule.type }}</div>
                  <div class="rule-sub">{{ rule.id }} · {{ checkerMap[rule.type]?.label || rule.type }}</div>
                </div>
                <el-tag size="small" :type="severityType(rule.severity)">{{ severityText(rule.severity) }}</el-tag>
                <el-switch
                  v-model="rule.enabled"
                  :disabled="quickToggling || working?.state === 'submitted' || !can('ruleset.edit')"
                  :loading="quickToggling && quickToggleRuleId === rule.id"
                  @click.stop
                  @change="toggleRuleEnabled(rule, $event)"
                />
                <div class="card-actions" @click.stop>
                  <el-button text :disabled="readOnly || !can('ruleset.edit')" @click="moveRule(rule, -1)">↑</el-button>
                  <el-button text :disabled="readOnly || !can('ruleset.edit')" @click="moveRule(rule, 1)">↓</el-button>
                  <el-button text :disabled="readOnly || !can('ruleset.edit')" @click="copyRule(rule)">复制</el-button>
                  <el-button text type="danger" :disabled="readOnly || !can('ruleset.edit')" @click="removeRule(rule)">删除</el-button>
                </div>
              </article>
            </el-tab-pane>

            <el-tab-pane label="样例测试" name="test">
              <el-alert title="样例测试不会创建正式审查记录，默认跳过 AI 检查。" type="info" :closable="false" />
              <el-form label-position="top" class="test-form">
                <el-form-item label="样例文档">
                  <div class="sample-picker">
                    <el-select
                      v-model="testDocumentId"
                      filterable
                      clearable
                      placeholder="选择最近上传的样例文档"
                      :loading="documentsLoading"
                    >
                      <el-option
                        v-for="document in documents"
                        :key="document.document_id"
                        :label="documentLabel(document)"
                        :value="document.document_id"
                      >
                        <div class="document-option">
                          <span>{{ document.filename }}</span>
                          <small>{{ formatSize(document.size) }} · {{ document.created_at || document.document_id }}</small>
                        </div>
                      </el-option>
                    </el-select>
                    <el-button @click="refreshDocuments" :loading="documentsLoading">刷新</el-button>
                  </div>
                  <div v-if="selectedTestDocument" class="selected-document">
                    已选：{{ selectedTestDocument.filename }} · {{ formatSize(selectedTestDocument.size) }}
                    <span>内部 ID：{{ selectedTestDocument.document_id }}</span>
                  </div>
                </el-form-item>
                <el-form-item label="上传新样例">
                  <el-upload
                    class="sample-upload"
                    drag
                    :auto-upload="false"
                    :show-file-list="false"
                    :on-change="uploadSampleDocument"
                    accept=".pdf,.docx"
                    :disabled="sampleUploading"
                  >
                    <div class="sample-upload-body">
                      <el-icon :size="34"><UploadFilled /></el-icon>
                      <span>{{ sampleUploading ? '样例上传中...' : '拖入 PDF/DOCX，或点击选择样例' }}</span>
                    </div>
                  </el-upload>
                </el-form-item>
                <el-form-item><el-checkbox v-model="testUseAi">同时运行 AI 检查（可能较慢）</el-checkbox></el-form-item>
                <el-form-item v-if="testUseAi"><el-checkbox v-model="testPrivacyConsent">我确认该样例允许发送至外部 AI 服务</el-checkbox></el-form-item>
                <el-button type="primary" :disabled="!working || !can('ruleset.test')" :loading="testing" @click="runTest">运行当前工作版本</el-button>
              </el-form>
              <div v-if="testResult" class="test-result">
                <el-statistic title="执行规则" :value="testResult.rules_executed" />
                <el-statistic title="发现问题" :value="testResult.issue_count" />
                <el-table :data="testResult.issues" max-height="300">
                  <el-table-column prop="rule_id" label="规则" width="90" />
                  <el-table-column prop="severity" label="级别" width="80" />
                  <el-table-column prop="message" label="问题" />
                </el-table>
              </div>
            </el-tab-pane>

            <el-tab-pane label="版本与审计" name="versions">
              <el-table :data="detail.versions">
                <el-table-column prop="version_number" label="版本" width="72">
                  <template #default="{ row }">v{{ row.version_number }}</template>
                </el-table-column>
                <el-table-column prop="state" label="状态" width="100" />
                <el-table-column prop="change_note" label="变更说明" />
                <el-table-column prop="created_at" label="创建时间" width="170" />
                <el-table-column label="操作" width="100">
                  <template #default="{ row }">
                    <el-button text :disabled="!can('ruleset.edit') || row.id === detail.active_version?.id || Boolean(detail.working_version)" @click="restoreVersion(row)">恢复</el-button>
                  </template>
                </el-table-column>
              </el-table>
              <h3>操作审计</h3>
              <el-timeline>
                <el-timeline-item v-for="event in detail.audit" :key="event.id" :timestamp="event.created_at">
                  {{ event.action }} · {{ event.actor }}
                </el-timeline-item>
              </el-timeline>
            </el-tab-pane>

            <el-tab-pane label="高级 YAML" name="yaml">
              <el-alert title="高级模式适合熟悉规则结构的管理员；保存前仍会执行相同校验。" type="warning" :closable="false" />
              <el-input v-model="yamlText" type="textarea" :disabled="readOnly" :autosize="{ minRows: 24, maxRows: 40 }" spellcheck="false" />
              <el-button class="yaml-save" type="primary" :disabled="readOnly || !can('ruleset.edit')" @click="saveYaml">校验并保存 YAML</el-button>
            </el-tab-pane>
          </el-tabs>
        </template>
        <el-empty v-else description="选择一个规则集开始管理">
          <el-button type="primary" @click="newDialog = true">新建规则集</el-button>
        </el-empty>
      </main>

      <aside v-if="tab === 'rules'" class="property-panel panel">
        <template v-if="selectedRule">
          <h2>规则属性</h2>
          <el-form label-position="top" :disabled="readOnly || !can('ruleset.edit')">
            <el-form-item label="规则 ID"><el-input v-model="selectedRule.id" /></el-form-item>
            <el-form-item label="检查器">
              <el-select v-model="selectedRule.type" filterable @change="resetParams">
                <el-option v-for="checker in checkers" :key="checker.type" :label="`${checker.group} · ${checker.label}`" :value="checker.type" />
              </el-select>
            </el-form-item>
            <el-form-item label="规则名称"><el-input v-model="selectedRule.description" /></el-form-item>
            <el-form-item label="严重程度">
              <el-radio-group v-model="selectedRule.severity">
                <el-radio-button value="error">错误</el-radio-button>
                <el-radio-button value="warning">警告</el-radio-button>
                <el-radio-button value="info">提示</el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="规则标签">
              <el-input :model-value="selectedRule.tags.join('，')" placeholder="硬性要求，篇幅" @update:model-value="selectedRule.tags = splitList($event)" />
            </el-form-item>
            <div class="param-title">规则依据</div>
            <div class="two-columns">
              <el-form-item label="依据文件"><el-input v-model="selectedRule.basis.document" /></el-form-item>
              <el-form-item label="版本"><el-input v-model="selectedRule.basis.version" /></el-form-item>
              <el-form-item label="条款"><el-input v-model="selectedRule.basis.clause" /></el-form-item>
              <el-form-item label="公开链接"><el-input v-model="selectedRule.basis.url" /></el-form-item>
            </div>
            <el-form-item label="依据摘录"><el-input v-model="selectedRule.basis.excerpt" type="textarea" :rows="2" /></el-form-item>
            <el-form-item label="人工复核边界"><el-input v-model="selectedRule.review_note" type="textarea" :rows="2" /></el-form-item>

            <template v-if="selectedRule.type === 'required_sections'">
              <div class="param-title">章节要求</div>
              <div v-for="(section, index) in selectedRule.params.sections" :key="index" class="section-row">
                <el-input v-model="section.title" placeholder="章节标题，可用 | 分隔别名" />
                <el-input-number v-model="section.min_words" :min="0" placeholder="最低字数" />
                <el-button text type="danger" @click="selectedRule.params.sections.splice(index, 1)">移除</el-button>
              </div>
              <el-button @click="selectedRule.params.sections.push({ title: '', min_words: 0 })">添加章节</el-button>
            </template>

            <template v-else-if="['page_limit', 'word_limit'].includes(selectedRule.type)">
              <div class="param-title">总量与分章节限制</div>
              <el-form-item :label="selectedRule.type === 'page_limit' ? '最大总页数' : '最大总字数'">
                <el-input-number v-model="selectedRule.params[selectedRule.type === 'page_limit' ? 'max_pages' : 'max_words']" :min="1" />
              </el-form-item>
              <el-form-item label="分章节限制">
                <KeyNumberMapEditor v-model="selectedRule.params.section_limits" key-placeholder="章节名称" :min="1" />
              </el-form-item>
            </template>

            <template v-else-if="selectedRule.type === 'budget_check'">
              <div class="param-title">预算参数</div>
              <el-form-item label="科目最高占比">
                <KeyNumberMapEditor v-model="selectedRule.params.max_ratio" key-placeholder="预算科目" :min="0" :max="1" :step="0.01" />
              </el-form-item>
              <el-form-item label="合计字段正则"><el-input v-model="selectedRule.params.total_field" /></el-form-item>
            </template>

            <template v-else-if="selectedRule.type === 'heading_numbering'">
              <div class="param-title">标题编号体系</div>
              <el-form-item label="跳过页码">
                <el-input :model-value="selectedRule.params.skip_pages.join('，')" @update:model-value="selectedRule.params.skip_pages = numberList($event)" />
              </el-form-item>
              <el-form-item label="编号要求"><el-switch v-model="selectedRule.params.require_numbering" active-text="所有标题必须编号" /></el-form-item>
              <el-form-item label="样式混用"><el-switch v-model="selectedRule.params.allow_mixed_styles" active-text="允许同级混用" /></el-form-item>
              <el-form-item label="单段阿拉伯编号层级"><el-input-number v-model="selectedRule.params.single_arabic_level" :min="1" :max="6" /></el-form-item>
              <el-form-item v-for="level in ['1', '2', '3']" :key="level" :label="`${level}级允许样式`">
                <el-select v-model="selectedRule.params.allowed_styles[level]" multiple filterable allow-create>
                  <el-option v-for="style in headingStyleOptions" :key="style" :label="style" :value="style" />
                </el-select>
              </el-form-item>
              <el-form-item label="最大问题数"><el-input-number v-model="selectedRule.params.max_issues" :min="1" /></el-form-item>
            </template>

            <template v-else-if="fieldBased">
              <div class="param-title">字段配置</div>
              <div v-for="(field, index) in selectedRule.params.fields" :key="index" class="nested-card">
                <el-input v-model="field.name" placeholder="字段名称" />
                <el-input :model-value="(field.aliases || []).join('，')" placeholder="别名，用逗号分隔" @update:model-value="field.aliases = splitList($event)" />
                <el-select v-if="selectedRule.type === 'field_format'" v-model="field.format" placeholder="格式">
                  <el-option v-for="format in formatOptions" :key="format" :label="format" :value="format" />
                </el-select>
                <el-input v-if="selectedRule.type === 'field_format'" v-model="field.pattern" placeholder="自定义正则（与内置格式二选一）" />
                <el-input v-if="selectedRule.type === 'field_format'" v-model="field.expected" placeholder="格式说明" />
                <el-switch v-if="selectedRule.type === 'required_fields'" v-model="field.required" active-text="必填" />
                <el-select v-if="selectedRule.type === 'cross_field_consistency'" v-model="field.normalize" placeholder="归一化">
                  <el-option v-for="normalizer in normalizeOptions" :key="normalizer" :label="normalizer" :value="normalizer" />
                </el-select>
                <el-input v-if="selectedRule.type === 'cross_field_consistency'" v-model="field.pattern" placeholder="取值正则（可选）" />
                <el-input v-if="selectedRule.type === 'cross_field_consistency'" v-model="field.validation_pattern" placeholder="格式验证正则（可选）" />
                <el-input-number v-if="selectedRule.type === 'cross_field_consistency'" v-model="field.min_occurrences" :min="2" />
                <el-button text type="danger" @click="selectedRule.params.fields.splice(index, 1)">移除</el-button>
              </div>
              <el-button @click="addField">添加字段</el-button>
              <template v-if="selectedRule.type === 'cross_field_consistency'">
                <div class="param-title">字段对比较</div>
                <div v-for="(comparison, index) in selectedRule.params.comparisons" :key="index" class="comparison-card">
                  <el-input v-model="comparison.left.name" placeholder="左侧字段" />
                  <el-input :model-value="(comparison.left.aliases || []).join('，')" placeholder="左侧别名" @update:model-value="comparison.left.aliases = splitList($event)" />
                  <el-input v-model="comparison.right.name" placeholder="右侧字段" />
                  <el-input :model-value="(comparison.right.aliases || []).join('，')" placeholder="右侧别名" @update:model-value="comparison.right.aliases = splitList($event)" />
                  <el-select v-model="comparison.normalize" placeholder="归一化">
                    <el-option v-for="normalizer in normalizeOptions" :key="normalizer" :label="normalizer" :value="normalizer" />
                  </el-select>
                  <el-input-number v-if="comparison.normalize === 'number'" v-model="comparison.tolerance" :min="0" :step="0.01" />
                  <el-button text type="danger" @click="selectedRule.params.comparisons.splice(index, 1)">移除比较</el-button>
                </div>
                <el-button @click="addComparison">添加字段对</el-button>
              </template>
            </template>

            <template v-else-if="selectedRule.type === 'layout_check'">
              <div class="param-title">页面与边距</div>
              <div class="two-columns">
                <el-form-item label="纸张"><el-select v-model="selectedRule.params.page.size"><el-option label="A4" value="A4" /><el-option label="A3" value="A3" /><el-option label="Letter" value="Letter" /></el-select></el-form-item>
                <el-form-item label="方向"><el-select v-model="selectedRule.params.page.orientation"><el-option label="纵向" value="portrait" /><el-option label="横向" value="landscape" /></el-select></el-form-item>
                <el-form-item label="左边距最小 mm"><el-input-number v-model="selectedRule.params.margins_mm.left_min" :min="0" /></el-form-item>
                <el-form-item label="右边距最小 mm"><el-input-number v-model="selectedRule.params.margins_mm.right_min" :min="0" /></el-form-item>
                <el-form-item label="上边距最小 mm"><el-input-number v-model="selectedRule.params.margins_mm.top_min" :min="0" /></el-form-item>
                <el-form-item label="下边距最小 mm"><el-input-number v-model="selectedRule.params.margins_mm.bottom_min" :min="0" /></el-form-item>
                <el-form-item label="正文最小字号"><el-input-number v-model="selectedRule.params.body.min_size" :min="1" /></el-form-item>
                <el-form-item label="正文最大字号"><el-input-number v-model="selectedRule.params.body.max_size" :min="1" /></el-form-item>
                <el-form-item label="最小行距 pt"><el-input-number v-model="selectedRule.params.body.line_spacing_min_pt" :min="0" /></el-form-item>
                <el-form-item label="最大行距 pt"><el-input-number v-model="selectedRule.params.body.line_spacing_max_pt" :min="0" /></el-form-item>
                <el-form-item label="首行缩进最小 pt"><el-input-number v-model="selectedRule.params.body.first_line_indent_min_pt" :min="0" /></el-form-item>
                <el-form-item label="首行缩进最大 pt"><el-input-number v-model="selectedRule.params.body.first_line_indent_max_pt" :min="0" /></el-form-item>
              </div>
              <div class="param-title">标题级别</div>
              <div v-for="level in ['1', '2', '3']" :key="level" class="heading-row">
                <span>{{ level }} 级</span>
                <el-input-number v-model="selectedRule.params.headings[level].min_size" :min="1" />
                <el-input-number v-model="selectedRule.params.headings[level].max_size" :min="1" />
                <el-checkbox v-model="selectedRule.params.headings[level].bold">粗体</el-checkbox>
                <el-select v-model="selectedRule.params.headings[level].alignment" clearable placeholder="对齐">
                  <el-option label="左对齐" value="left" /><el-option label="居中" value="center" /><el-option label="右对齐" value="right" />
                </el-select>
              </div>
            </template>

            <template v-else>
              <div class="param-title">检查器参数</div>
              <el-form-item v-for="(schema, key) in currentProperties" :key="key" :label="schema.title || key">
                <el-switch v-if="schema.type === 'boolean'" v-model="selectedRule.params[key]" />
                <el-input-number v-else-if="schema.type === 'integer' || schema.type === 'number'" v-model="selectedRule.params[key]" :min="schema.minimum" />
                <el-input v-else-if="schema.type === 'string'" v-model="selectedRule.params[key]" />
                <el-input v-else-if="schema.type === 'array' && schema.items?.type === 'string'" :model-value="(selectedRule.params[key] || []).join('，')" @update:model-value="selectedRule.params[key] = splitList($event)" />
                <el-input v-else :model-value="jsonValue(selectedRule.params[key])" type="textarea" :rows="3" @change="setJsonParam(key, $event)" />
              </el-form-item>
            </template>
          </el-form>
        </template>
        <el-empty v-else description="选中左侧某条规则后显示详情设置" />
      </aside>
    </div>

    <el-dialog v-model="submitDialog" title="提交规则审核" width="min(520px, 94vw)">
      <el-alert title="提交前必须完成当前版本的样例测试；提交后内容将锁定。" type="info" :closable="false" />
      <el-form label-position="top" class="submit-form">
        <el-form-item label="指定审核员" required>
          <el-select v-model="submitForm.assigned_reviewer" style="width: 100%" placeholder="请选择负责审核和发布的用户">
            <el-option v-for="reviewer in reviewers" :key="reviewer.username" :label="`${reviewer.display_name}（${reviewer.username}）`" :value="reviewer.username" />
          </el-select>
        </el-form-item>
        <el-form-item label="提交说明"><el-input v-model="submitForm.note" type="textarea" :rows="3" placeholder="说明本次规则调整内容" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="submitDialog = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitForReview">确认提交</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="newDialog" title="新建规则集" width="520px">
      <el-form label-position="top">
        <el-form-item label="规则集 ID"><el-input v-model="newForm.ruleset" placeholder="如 campus_2027" /></el-form-item>
        <el-form-item label="名称"><el-input v-model="newForm.name" /></el-form-item>
        <el-form-item label="说明"><el-input v-model="newForm.description" type="textarea" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="newDialog = false">取消</el-button><el-button type="primary" @click="createRuleset">创建草稿</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { UploadFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import KeyNumberMapEditor from '../components/rules/KeyNumberMapEditor.vue'
import {
  actOnManagedVersion, cloneManagedRuleset, createManagedRuleset, getCheckerCatalog,
  deleteManagedRuleset, deleteManagedVersion, getManagedRuleset, getManagedRulesets,
  getCurrentUser, getMe, listDocuments, listReviewers, restoreManagedRuleset, saveManagedVersion, saveManagedYaml,
  setManagedRulesetEnabled, uploadDocument, validateManagedRuleset,
} from '../services/api'

const rulesets = ref([])
const checkers = ref([])
const detail = ref(null)
const model = reactive({ ruleset: '', name: '', description: '', rules: [] })
const selectedId = ref('')
const selectedRule = ref(null)
const search = ref('')
const ruleSearch = ref('')
const tab = ref('rules')
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const quickToggling = ref(false)
const quickToggleRuleId = ref('')
const yamlText = ref('')
const testDocumentId = ref('')
const documents = ref([])
const documentsLoading = ref(false)
const sampleUploading = ref(false)
const testUseAi = ref(false)
const testPrivacyConsent = ref(false)
const testResult = ref(null)
const adminToken = ref(sessionStorage.getItem('adminToken') || '')
const currentUser = ref(getCurrentUser())
const reviewers = ref([])
const submitDialog = ref(false)
const submitting = ref(false)
const submitForm = reactive({ assigned_reviewer: '', note: '' })
const newDialog = ref(false)
const newForm = reactive({ ruleset: '', name: '', description: '' })
const formatOptions = ['email', 'cn_mobile', 'phone', 'date', 'cn_id', 'number', 'project_code']
const normalizeOptions = ['text', 'casefold', 'phone', 'date', 'number', 'list']
const headingStyleOptions = ['chinese', 'chapter', 'arabic', 'chinese_parenthesized', 'arabic_parenthesized', 'decimal']

const checkerMap = computed(() => Object.fromEntries(checkers.value.map(item => [item.type, item])))
const filteredRulesets = computed(() => rulesets.value.filter(item => `${item.id}${item.name}`.toLowerCase().includes(search.value.toLowerCase())))
const visibleRules = computed(() => model.rules.filter(rule => `${rule.id}${rule.type}${rule.description}`.toLowerCase().includes(ruleSearch.value.toLowerCase())))
const working = computed(() => detail.value?.working_version || null)
const readOnly = computed(() => !working.value || ['submitted', 'approved'].includes(working.value.state))
const versionLabel = computed(() => working.value ? `工作版本 v${working.value.version_number} · ${working.value.state}` : `当前发布 v${detail.value?.active_version?.version_number || '-'}`)
const fieldBased = computed(() => ['required_fields', 'field_format', 'cross_field_consistency'].includes(selectedRule.value?.type))
const currentProperties = computed(() => checkerMap.value[selectedRule.value?.type]?.schema?.properties || {})
const canDiscardDraft = computed(() => working.value && ['draft', 'rejected'].includes(working.value.state))
const canDeleteRuleset = computed(() => detail.value && !detail.value.active_version && canDiscardDraft.value)
const selectedTestDocument = computed(() => documents.value.find(item => item.document_id === testDocumentId.value))
const isAssignedReviewer = computed(() => working.value?.assigned_reviewer === currentUser.value?.username)

onMounted(async () => {
  try { currentUser.value = await getMe() } catch {}
  if (can('ruleset.submit')) {
    try { reviewers.value = await listReviewers() } catch {}
  }
  await initialize()
})

function can(capability) {
  return currentUser.value?.capabilities?.includes(capability) || (!currentUser.value && Boolean(adminToken.value))
}

async function initialize() {
  try {
    [checkers.value, rulesets.value] = await Promise.all([
      getCheckerCatalog(adminToken.value),
      getManagedRulesets(adminToken.value),
    ])
    await refreshDocuments(false)
    if (rulesets.value.length) await loadRuleset(rulesets.value[0].id)
  } catch (error) { ElMessage.error(error.message) }
}

async function refreshList() { rulesets.value = await getManagedRulesets(adminToken.value) }
async function loadRuleset(id) {
  loading.value = true
  try {
    detail.value = await getManagedRuleset(id, adminToken.value)
    selectedId.value = id
    const version = detail.value.working_version || detail.value.active_version
    const source = deepClone(version?.data || {
      ruleset: id, name: detail.value.name, description: detail.value.description, rules: [],
    })
    model.ruleset = source.ruleset || id
    model.name = source.name || detail.value.name || id
    model.description = source.description || detail.value.description || ''
    model.rules.splice(0, model.rules.length, ...(source.rules || []))
    for (const rule of model.rules) if (rule.enabled === undefined) rule.enabled = true
    yamlText.value = version?.yaml || ''
    selectedRule.value = null
    normalizeSelectedRule()
  } catch (error) { ElMessage.error(error.message) }
  finally { loading.value = false }
}

async function createDraft() {
  try {
    await cloneManagedRuleset(detail.value.id, detail.value.active_version?.id, adminToken.value)
    await loadRuleset(detail.value.id); await refreshList(); ElMessage.success('编辑草稿已创建')
  } catch (error) { ElMessage.error(error.message) }
}

async function toggleRuleEnabled(rule, enabled) {
  // Existing editable drafts keep the normal explicit "save draft" workflow.
  if (!can('ruleset.edit')) return
  if (working.value || quickToggling.value) return
  const ruleId = rule.id
  quickToggling.value = true
  quickToggleRuleId.value = ruleId
  try {
    await cloneManagedRuleset(detail.value.id, detail.value.active_version?.id, adminToken.value)
    await loadRuleset(detail.value.id)
    const target = model.rules.find(item => item.id === ruleId)
    if (!target) throw new Error(`未找到规则 ${ruleId}`)
    target.enabled = Boolean(enabled)
    const response = await saveManagedVersion(
      working.value.id,
      deepClone(model),
      working.value.revision,
      adminToken.value,
    )
    detail.value.working_version = response.version
    yamlText.value = response.version.yaml
    selectedRule.value = target
    await refreshList()
    ElMessage.success(`规则 ${ruleId} 已${enabled ? '开启' : '关闭'}并保存到草稿`)
  } catch (error) {
    try { await loadRuleset(detail.value.id) } catch {}
    ElMessage.error(error.message || String(error))
  } finally {
    quickToggling.value = false
    quickToggleRuleId.value = ''
  }
}

async function saveDraft() {
  if (!working.value) return
  saving.value = true
  try {
    const response = await saveManagedVersion(working.value.id, deepClone(model), working.value.revision, adminToken.value)
    detail.value.working_version = response.version
    yamlText.value = response.version.yaml
    await refreshList(); ElMessage.success('草稿已保存')
  } catch (error) { ElMessage.error(error.message) }
  finally { saving.value = false }
}

async function saveYaml() {
  try {
    const response = await saveManagedYaml(working.value.id, yamlText.value, working.value.revision, adminToken.value)
    detail.value.working_version = response.version
    Object.assign(model, deepClone(response.version.data)); selectedRule.value = null; normalizeSelectedRule()
    ElMessage.success('YAML 已校验并保存')
  } catch (error) { ElMessage.error(error.message) }
}

async function validateCurrent() {
  try {
    const result = await validateManagedRuleset(deepClone(model), adminToken.value)
    if (result.valid) ElMessage.success(result.warnings.length ? `校验通过，${result.warnings.length} 项提示` : '校验通过')
    else ElMessage.error(result.errors.map(item => `${item.path}: ${item.message}`).join('；'))
  } catch (error) { ElMessage.error(error.message) }
}

async function lifecycleAction(command) {
  try {
    if (command === 'toggle-status') {
      const enabled = detail.value.status === 'disabled'
      await setManagedRulesetEnabled(detail.value.id, enabled, adminToken.value)
    } else if (command === 'delete-draft') {
      await deleteCurrentDraft()
      return
    } else if (command === 'delete-ruleset') {
      await deleteCurrentRuleset()
      return
    } else {
      if (!working.value) throw new Error('请先创建工作草稿')
      if (command === 'submit') {
        submitForm.assigned_reviewer = reviewers.value[0]?.username || ''
        submitForm.note = ''
        submitDialog.value = true
        return
      }
      let note = ''
      if (command === 'approve') {
        const result = await ElMessageBox.prompt('可填写审核意见。审核通过后版本进入待发布状态。', '审核通过', { inputType: 'textarea' })
        note = result.value || ''
      }
      if (command === 'reject') {
        const result = await ElMessageBox.prompt('请填写明确的整改原因，维护员将根据该意见修改。', '驳回整改', {
          inputType: 'textarea', inputValidator: value => Boolean(value?.trim()) || '驳回原因不能为空',
        })
        note = result.value
      }
      if (command === 'publish') {
        await ElMessageBox.confirm('该版本已审核通过。发布后，新审查任务将立即使用此版本。是否继续？', '确认发布')
      }
      await actOnManagedVersion(working.value.id, command, adminToken.value, { note })
    }
    await loadRuleset(detail.value.id); await refreshList(); ElMessage.success('操作成功')
  } catch (error) { if (error !== 'cancel') ElMessage.error(error.message || String(error)) }
}

async function submitForReview() {
  if (!submitForm.assigned_reviewer) return ElMessage.warning('请选择审核员')
  submitting.value = true
  try {
    await actOnManagedVersion(working.value.id, 'submit', adminToken.value, {
      assigned_reviewer: submitForm.assigned_reviewer,
      note: submitForm.note,
    })
    submitDialog.value = false
    await loadRuleset(detail.value.id)
    await refreshList()
    ElMessage.success('已提交给指定审核员')
  } catch (error) { ElMessage.error(error.message) }
  finally { submitting.value = false }
}

async function deleteCurrentDraft() {
  if (!working.value) return
  await ElMessageBox.confirm(
    '当前草稿会被删除，已发布版本不受影响。是否继续？',
    '删除当前草稿',
    { type: 'warning' },
  )
  await deleteManagedVersion(working.value.id, adminToken.value)
  await loadRuleset(detail.value.id)
  await refreshList()
  ElMessage.success('当前草稿已删除')
}

async function deleteCurrentRuleset() {
  if (!detail.value) return
  await ElMessageBox.confirm(
    `规则集 ${detail.value.name || detail.value.id} 从未发布，将被彻底删除。是否继续？`,
    '删除规则集',
    { type: 'warning' },
  )
  const deletedId = detail.value.id
  await deleteManagedRuleset(deletedId, adminToken.value)
  detail.value = null
  selectedId.value = ''
  selectedRule.value = null
  model.rules.splice(0, model.rules.length)
  await refreshList()
  if (rulesets.value.length) await loadRuleset(rulesets.value[0].id)
  ElMessage.success(`规则集 ${deletedId} 已删除`)
}

function addRule() {
  const type = checkers.value[0]?.type || 'required_fields'
  const rule = { id: nextRuleId(), type, severity: 'warning', description: '', enabled: true, basis: '', tags: [], review_note: '', params: defaultsFor(type) }
  model.rules.push(rule); selectedRule.value = rule
}
function nextRuleId() {
  let number = model.rules.length + 1
  while (model.rules.some(rule => rule.id === `R${String(number).padStart(3, '0')}`)) number += 1
  return `R${String(number).padStart(3, '0')}`
}
function defaultsFor(type) {
  if (type === 'required_sections') return { sections: [] }
  if (['page_limit', 'word_limit'].includes(type)) return { section_limits: {} }
  if (type === 'budget_check') return { max_ratio: {}, total_field: '合计|总计' }
  if (type === 'heading_numbering') return { skip_pages: [1], require_numbering: false, allow_mixed_styles: false, single_arabic_level: 1, allowed_styles: { '1': [], '2': [], '3': [] }, max_issues: 20 }
  if (['required_fields', 'field_format'].includes(type)) return { fields: [] }
  if (type === 'cross_field_consistency') return { fields: [], comparisons: [] }
  if (type === 'layout_check') return { page: { size: 'A4', orientation: 'portrait' }, margins_mm: { left_min: 15, right_min: 15, top_min: 15, bottom_min: 15 }, body: { min_size: 9, max_size: 14, line_spacing_min_pt: 11, line_spacing_max_pt: 32, first_line_indent_min_pt: 0, first_line_indent_max_pt: 36 }, headings: { '1': { min_size: 12, max_size: 22, bold: true, alignment: '' }, '2': { min_size: 10, max_size: 20, bold: false, alignment: '' }, '3': { min_size: 9, max_size: 18, bold: false, alignment: '' } }, max_issues: 20 }
  const params = {}
  for (const [key, schema] of Object.entries(checkerMap.value[type]?.schema?.properties || {})) {
    if (schema.default !== undefined) params[key] = deepClone(schema.default)
    else if (schema.type === 'array') params[key] = []
    else if (schema.type === 'object') params[key] = {}
  }
  return params
}
function resetParams() { selectedRule.value.params = defaultsFor(selectedRule.value.type); normalizeSelectedRule() }
function selectRule(rule) { selectedRule.value = rule; normalizeSelectedRule() }
function moveRule(rule, offset) { const index = model.rules.indexOf(rule); const target = index + offset; if (target < 0 || target >= model.rules.length) return; model.rules.splice(index, 1); model.rules.splice(target, 0, rule) }
function copyRule(rule) { const copy = deepClone(rule); copy.id = `${rule.id}_COPY`; model.rules.splice(model.rules.indexOf(rule) + 1, 0, copy); selectedRule.value = copy }
function removeRule(rule) { model.rules.splice(model.rules.indexOf(rule), 1); if (selectedRule.value === rule) selectedRule.value = null }
function addField() { selectedRule.value.params.fields ||= []; selectedRule.value.params.fields.push({ name: '', aliases: [], ...(selectedRule.value.type === 'field_format' ? { format: 'email' } : {}) }) }
function addComparison() { selectedRule.value.params.comparisons ||= []; selectedRule.value.params.comparisons.push({ left: { name: '', aliases: [] }, right: { name: '', aliases: [] }, normalize: 'text', tolerance: 0 }) }
function normalizeSelectedRule() {
  if (!selectedRule.value) return
  selectedRule.value.params ||= {}
  selectedRule.value.tags ||= []
  selectedRule.value.review_note ||= ''
  const basis = selectedRule.value.basis
  selectedRule.value.basis = typeof basis === 'object' && basis !== null
    ? { document: '', version: '', clause: '', excerpt: '', url: '', ...basis }
    : { document: '', version: '', clause: '', excerpt: String(basis || ''), url: '' }
  if (selectedRule.value.type === 'required_sections') selectedRule.value.params.sections ||= []
  if (['page_limit', 'word_limit'].includes(selectedRule.value.type)) selectedRule.value.params.section_limits ||= {}
  if (selectedRule.value.type === 'budget_check') {
    selectedRule.value.params.max_ratio ||= {}
    selectedRule.value.params.total_field ||= '合计|总计'
  }
  if (selectedRule.value.type === 'heading_numbering') {
    const defaults = defaultsFor('heading_numbering')
    selectedRule.value.params = { ...defaults, ...selectedRule.value.params }
    selectedRule.value.params.allowed_styles ||= {}
    for (const level of ['1', '2', '3']) selectedRule.value.params.allowed_styles[level] ||= []
  }
  if (fieldBased.value) selectedRule.value.params.fields ||= []
  if (selectedRule.value.type === 'required_fields') {
    for (const field of selectedRule.value.params.fields) if (field.required === undefined) field.required = true
  }
  if (selectedRule.value.type === 'cross_field_consistency') {
    for (const field of selectedRule.value.params.fields) {
      field.normalize ||= 'text'
      field.min_occurrences ||= 2
    }
  }
  if (selectedRule.value.type === 'cross_field_consistency') selectedRule.value.params.comparisons ||= []
  if (selectedRule.value.type === 'layout_check') {
    const defaults = defaultsFor('layout_check')
    selectedRule.value.params.page = { ...defaults.page, ...(selectedRule.value.params.page || {}) }
    selectedRule.value.params.margins_mm = { ...defaults.margins_mm, ...(selectedRule.value.params.margins_mm || {}) }
    selectedRule.value.params.body = { ...defaults.body, ...(selectedRule.value.params.body || {}) }
    selectedRule.value.params.headings ||= {}
    for (const level of ['1', '2', '3']) selectedRule.value.params.headings[level] = { ...defaults.headings[level], ...(selectedRule.value.params.headings[level] || {}) }
  }
}

async function runTest() {
  if (!testDocumentId.value) return ElMessage.warning('请填写样例文档 ID')
  testing.value = true
  try {
    testResult.value = await actOnManagedVersion(working.value.id, 'test', adminToken.value, { document_id: testDocumentId.value, use_ai: testUseAi.value, privacy_consent: testPrivacyConsent.value })
    await loadRuleset(detail.value.id)
    ElMessage.success('样例测试完成')
  } catch (error) { ElMessage.error(error.message) }
  finally { testing.value = false }
}

async function restoreVersion(row) {
  try {
    await ElMessageBox.confirm(`将历史 v${row.version_number} 克隆为新草稿；仍需样例测试、提交审核后才能发布。`, '恢复历史版本')
    await restoreManagedRuleset(detail.value.id, row.id, adminToken.value)
    await loadRuleset(detail.value.id); await refreshList(); ElMessage.success('历史版本已恢复为新草稿')
  } catch (error) { if (error !== 'cancel') ElMessage.error(error.message || String(error)) }
}

async function refreshDocuments(showMessage = true) {
  documentsLoading.value = true
  try {
    documents.value = await listDocuments(50)
    if (showMessage) ElMessage.success('样例文档列表已刷新')
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    documentsLoading.value = false
  }
}

async function uploadSampleDocument(uploadFile) {
  const raw = uploadFile.raw
  if (!raw) return
  sampleUploading.value = true
  try {
    const uploaded = await uploadDocument(raw)
    await refreshDocuments(false)
    testDocumentId.value = uploaded.document_id
    ElMessage.success(`已上传并选择样例：${uploaded.filename}`)
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    sampleUploading.value = false
  }
}

async function createRuleset() {
  try {
    await createManagedRuleset({ ...newForm, rules: [] }, adminToken.value)
    newDialog.value = false; await refreshList(); await loadRuleset(newForm.ruleset)
    Object.assign(newForm, { ruleset: '', name: '', description: '' }); ElMessage.success('规则集草稿已创建')
  } catch (error) { ElMessage.error(error.message) }
}

async function authenticate() {
  const value = adminToken.value.trim()
  if (value) sessionStorage.setItem('adminToken', value)
  else sessionStorage.removeItem('adminToken')
  await initialize()
}
function splitList(value) { return String(value || '').split(/[，,]/).map(item => item.trim()).filter(Boolean) }
function deepClone(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)) }
function numberList(value) { return splitList(value).map(Number).filter(item => Number.isInteger(item) && item > 0) }
function jsonValue(value) { try { return JSON.stringify(value ?? {}, null, 2) } catch { return '{}' } }
function setJsonParam(key, value) { try { selectedRule.value.params[key] = JSON.parse(value) } catch { ElMessage.warning('JSON 参数格式无效') } }
function severityText(value) { return { error: '错误', warning: '警告', info: '提示' }[value] || value }
function severityType(value) { return { error: 'danger', warning: 'warning', info: 'info' }[value] || 'info' }
function workflowStateLabel(state) {
  return { draft: '草稿编辑中', submitted: '待指定审核员处理', approved: '审核通过·待发布', rejected: '已驳回·待整改' }[state] || state
}
function workflowTagType(state) { return { draft: 'info', submitted: 'warning', approved: 'success', rejected: 'danger' }[state] || 'info' }
function statusText(item) {
  if (item.status === 'disabled') return '已停用'
  if (item.working_version?.state === 'submitted') return '待审核'
  if (item.working_version?.state === 'approved') return '待发布'
  if (item.working_version?.state === 'rejected') return '待整改'
  if (item.working_version) return '有草稿'
  return '已发布'
}
function documentLabel(document) { return `${document.filename}（${formatSize(document.size)}）` }
function formatSize(size) {
  const value = Number(size || 0)
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${value} B`
}
</script>

<style scoped>
.rule-studio { max-width: 1680px; margin: 0 auto; }
.studio-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.studio-header h1 { margin: 0; font-size: 24px; color: #17324d; }
.studio-header p { margin: 5px 0 0; color: #6b7f92; }
.header-actions { display: flex; gap: 10px; width: 440px; }
.studio-grid { display: grid; grid-template-columns: 260px minmax(0, 1fr); gap: 14px; align-items: start; }
.studio-grid.has-property { grid-template-columns: 260px minmax(0, 1fr) minmax(340px, 370px); }
.studio-grid > * { min-width: 0; }
.panel { background: #fff; border: 1px solid #e3eaf1; border-radius: 12px; box-shadow: 0 4px 18px rgb(28 67 103 / 5%); }
.ruleset-nav { padding: 14px; position: sticky; top: 12px; }
.nav-create { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 12px; padding: 12px; border: 1px solid #dcebf8; border-radius: 8px; background: #f6fbff; }
.nav-create strong { display: block; color: #24445f; }
.nav-create span { display: block; margin-top: 3px; color: #7890a5; font-size: 12px; line-height: 1.4; }
.nav-tip { margin-top: 10px; }
.nav-list { margin-top: 12px; display: grid; gap: 6px; max-height: calc(100vh - 220px); overflow: auto; }
.nav-item { border: 0; background: transparent; border-radius: 9px; padding: 11px; text-align: left; cursor: pointer; color: #314a60; }
.nav-item:hover, .nav-item.active { background: #eaf4ff; color: #155b97; }
.nav-name { display: block; font-weight: 650; line-height: 1.4; }
.nav-meta { display: flex; gap: 7px; align-items: center; margin-top: 7px; font-size: 12px; color: #8494a4; }
.rule-workspace { min-width: 0; min-height: 650px; padding: 18px; }
.rule-workspace :deep(.el-table) { max-width: 100%; }
.workspace-title { display: flex; justify-content: space-between; gap: 15px; align-items: center; }
.workflow-panel { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin: 14px 0 4px; padding: 11px 13px; border: 1px solid #dce8f2; border-radius: 9px; background: #f7fbfe; color: #557086; font-size: 13px; }
.workflow-panel .rejection { width: 100%; color: #c45656; }
.submit-form { margin-top: 16px; }
.name-input { width: min(420px, 100%); }
.name-input :deep(.el-input__wrapper) { box-shadow: none; padding-left: 0; font-size: 20px; font-weight: 650; }
.muted, .rule-sub { color: #8494a4; font-size: 12px; }
.toolbar, .rule-toolbar { display: flex; gap: 8px; }
.rule-toolbar { margin-bottom: 12px; }
.rule-toolbar .el-input { flex: 1; }
.rule-card { display: flex; gap: 11px; align-items: center; padding: 13px; margin-bottom: 8px; border: 1px solid #e4eaf0; border-radius: 10px; cursor: pointer; transition: .15s; }
.rule-card:hover, .rule-card.selected { border-color: #6faee2; background: #f5faff; }
.rule-card.disabled { opacity: .6; }
.rule-index { width: 28px; height: 28px; display: grid; place-items: center; border-radius: 8px; background: #eef3f7; color: #658096; font-size: 12px; }
.rule-main { min-width: 0; flex: 1; }
.rule-title { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card-actions { display: none; white-space: nowrap; }
.rule-card:hover .card-actions { display: flex; }
.property-panel { padding: 17px; position: sticky; top: 12px; max-height: calc(100vh - 105px); overflow: auto; }
.property-panel h2 { margin: 0 0 14px; font-size: 18px; }
.property-panel :deep(.el-select) { width: 100%; }
.param-title { margin: 12px 0 9px; padding-top: 12px; border-top: 1px solid #edf0f3; font-weight: 650; color: #2f526f; }
.nested-card { display: grid; gap: 7px; padding: 10px; margin-bottom: 8px; background: #f7f9fb; border-radius: 8px; }
.section-row { display: grid; grid-template-columns: 1fr 130px auto; gap: 8px; margin-bottom: 8px; align-items: center; }
.comparison-card { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; padding: 10px; margin-bottom: 8px; background: #f7f9fb; border-radius: 8px; }
.heading-row { display: grid; grid-template-columns: 36px 1fr 1fr auto 1fr; gap: 7px; align-items: center; margin-bottom: 8px; }
.two-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 0 10px; }
.test-form { max-width: 600px; margin-top: 18px; }
.sample-picker { display: flex; gap: 8px; width: 100%; }
.sample-picker .el-select { flex: 1; }
.document-option { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
.document-option small { color: #8a9bad; }
.selected-document { margin-top: 8px; color: #526f88; font-size: 12px; line-height: 1.6; }
.selected-document span { display: block; color: #8a9bad; }
.sample-upload { width: 100%; }
.sample-upload-body { display: grid; place-items: center; gap: 6px; color: #5f7890; }
.test-result { display: grid; grid-template-columns: 150px 150px 1fr; gap: 16px; margin-top: 20px; align-items: start; }
.yaml-save { margin-top: 12px; }
@media (max-width: 980px) { .studio-header { align-items: flex-start; gap: 12px; flex-direction: column; } .header-actions { width: 100%; } .studio-grid, .studio-grid.has-property { grid-template-columns: 1fr; } .property-panel { grid-column: 1; position: static; max-height: none; } .ruleset-nav { position: static; max-height: none; } .workspace-title { align-items: flex-start; flex-direction: column; } }
</style>
