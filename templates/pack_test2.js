let verifiedPackages = [];  // 已二次扫描的包裹
    let dangerPackages = [];    // 标记为敏货的包裹
    let selectedPackages = [];  // 手动选择的包裹
    let autoSubmitMode = false;  // 自动提交模式
    let splitMode = false;       // 单包裹拆分模式
    const totalPackages = 0;
    const customerId = 0;  // 当前客户ID
    const SESSION_KEY = 'pack_scan_' + customerId;

    // 现有打包弹窗状态
    let _pendingPackPayload = null;   // 待处理的打包参数（allPackages, batchId 等）
    let _selectedAppendTarget = null; // 已选中追加目标 packed record id

    // ========== 扫描状态暂存/恢复 ==========
    function saveScanState() {
        // 至少扫描了一个包裹才保存
        if (verifiedPackages.length === 0) {
            localStorage.removeItem(SESSION_KEY);
            return;
        }
        const state = {
            customerId: customerId,
            verified: verifiedPackages.map(p => ({ id: parseInt(p.id), tracking: p.tracking, seq: parseInt(p.seq) || 0 })),
            danger: dangerPackages.map(p => ({ id: parseInt(p.id), tracking: p.tracking, seq: parseInt(p.seq) || 0 })),
            savedAt: new Date().toISOString(),
            expiresAt: Date.now() + 24 * 60 * 60 * 1000  // 24小时过期
        };
        localStorage.setItem(SESSION_KEY, JSON.stringify(state));
    }

    function restoreScanState() {
        const raw = localStorage.getItem(SESSION_KEY);
        if (!raw) return false;
        try {
            const state = JSON.parse(raw);

            // Check expiry
            if (state.expiresAt && Date.now() > state.expiresAt) {
                localStorage.removeItem(SESSION_KEY);
                return false;
            }

            if (state.customerId !== customerId) return false;
            if (!state.verified || state.verified.length === 0) return false;

            // Build set of current package IDs (as numbers)
            const currentIds = new Set();
            document.querySelectorAll('.package-item').forEach(item => {
                currentIds.add(parseInt(item.dataset.id));
            });

            // Filter saved state to only packages that still exist on this page
            verifiedPackages = state.verified.filter(p => currentIds.has(parseInt(p.id)));
            dangerPackages = state.danger ? state.danger.filter(p => currentIds.has(parseInt(p.id))) : [];

            // Update UI for each restored package
            verifiedPackages.forEach(p => {
                updatePackageStatus(parseInt(p.id), 'verified');
            });
            dangerPackages.forEach(p => {
                updatePackageStatus(parseInt(p.id), 'danger');
            });
            updateScanCounts();
            updateDistribution();

            // Enable danger section if needed
            if (dangerPackages.length > 0 || verifiedPackages.length >= totalPackages) {
                document.getElementById('danger-scan-section').style.opacity = '1';
                document.getElementById('danger-scan-section').style.pointerEvents = 'auto';
            }

            showToast('已恢复扫描状态: ' + verifiedPackages.length + ' 件', 'info');
            return true;
        } catch (e) {
            console.error('restoreScanState error:', e);
            localStorage.removeItem(SESSION_KEY);
            return false;
        }
    }

    function clearScanState() {
        localStorage.removeItem(SESSION_KEY);
    }

    // ========== 初始化 ==========
    // 先尝试恢复上次扫描状态
    const restored = restoreScanState();

    // 如果没有恢复成功，正常初始化（包含退回重打包的敏货自动标记）
    if (!restored) {
        document.querySelectorAll('.package-item').forEach(item => {
            const pkgId = parseInt(item.id.replace('pkg-', ''));
            const isDanger = item.dataset.isDanger === 'true';
            const tracking = item.dataset.tracking;
            const seq = parseInt(item.dataset.seq) || 0;

            // 退回重打包的包裹：保留之前的货物性质标记
            if (isDanger) {
                // 自动标记为已验证（之前批次已扫描过）
                if (!verifiedPackages.some(p => p.id === pkgId)) {
                    verifiedPackages.push({ id: pkgId, tracking: tracking, seq: seq });
                }
                // 自动标记为敏货（保留之前的敏货标记，无需重新扫描）
                if (!dangerPackages.some(p => p.id === pkgId)) {
                    dangerPackages.push({ id: pkgId, tracking: tracking, seq: seq });
                }
                updatePackageStatus(pkgId, 'danger');
            } else {
                updatePackageStatus(pkgId, 'unverified');
            }
        });

        // 如果有预标记的敏货，启用敏货扫描区
        if (dangerPackages.length > 0) {
            document.getElementById('danger-scan-section').style.opacity = '1';
            document.getElementById('danger-scan-section').style.pointerEvents = 'auto';
            updateScanCounts();
            updateDistribution();
        }
    }

    // 自动提交防抖计时器
    let autoSubmitTimer = null;
    const AUTO_SUBMIT_DELAY = 300; // 300ms防抖，适合扫码枪

    // 切换自动提交模式
    function toggleAutoSubmit() {
        autoSubmitMode = document.getElementById('auto-submit-mode').checked;
        const input1 = document.getElementById('normal-verify-input');
        const input2 = document.getElementById('danger-verify-input');
        if (autoSubmitMode) {
            input1.placeholder = '📷 扫码自动确认（无需按Enter）';
            input2.placeholder = '📷 扫码自动标记敏货';
        } else {
            input1.placeholder = '📷 扫描快递单号确认包裹存在';
            input2.placeholder = '📷 扫描需要标记为敏货的包裹';
        }
    }

    // 单包裹拆分模式
    function toggleSplitMode() {
        splitMode = document.getElementById('enable-split').checked;
        if (splitMode) {
            showToast('已开启拆分模式：该包裹将打包为普货+敏货两箱', 'info');
            // 自动启用敏货区
            verifiedPackages = []; dangerPackages = [];
            clearSelection();
        } else {
            showToast('已关闭拆分模式', 'info');
        }
    }

    // 处理输入（手动Enter或自动防抖提交）
    async function handleNormalVerify(event) {
        const input = event.target;
        const tracking = input.value.trim();
        if (!tracking) return;

        if (event.key === 'Enter') {
            // 手动模式按Enter直接提交
            await verifyPackage(tracking, 'verify');
            input.value = '';
        } else if (autoSubmitMode) {
            // 自动模式：防抖提交
            if (autoSubmitTimer) clearTimeout(autoSubmitTimer);
            autoSubmitTimer = setTimeout(async () => {
                const currentTracking = input.value.trim();
                if (currentTracking) {
                    await verifyPackage(currentTracking, 'verify');
                    input.value = '';
                }
            }, AUTO_SUBMIT_DELAY);
        }
    }

    async function handleDangerVerify(event) {
        const input = event.target;
        const tracking = input.value.trim();
        if (!tracking) return;

        if (event.key === 'Enter') {
            await verifyPackage(tracking, 'danger');
            input.value = '';
        } else if (autoSubmitMode) {
            if (autoSubmitTimer) clearTimeout(autoSubmitTimer);
            autoSubmitTimer = setTimeout(async () => {
                const currentTracking = input.value.trim();
                if (currentTracking) {
                    await verifyPackage(currentTracking, 'danger');
                    input.value = '';
                }
            }, AUTO_SUBMIT_DELAY);
        }
    }

    // 快速全扫
    async function quickVerifyAll() {
        if (isActionLocked()) return;
        if (verifiedPackages.length >= totalPackages) {
            showToast('所有包裹已扫描完成', 'info');
            return;
        }
        lockAction();

        try {
            // 获取所有未扫描的包裹
            const unverified = document.querySelectorAll('.package-item[data-status="unverified"]');
            if (unverified.length === 0) {
                showToast('没有待扫描的包裹', 'info');
                return;
            }

            let scanned = 0;
            for (const item of unverified) {
                const tracking = item.dataset.tracking;
                if (!verifiedPackages.some(p => p.tracking === tracking)) {
                    await verifyPackage(tracking, 'verify');
                    scanned++;
                    await new Promise(r => setTimeout(r, 100)); // 防止请求过快
                }
            }
            showToast(`快速扫描完成：${scanned} 件`, 'success');
        } catch (e) {
            showToast(e.message || '扫描失败', 'error');
        } finally {
            unlockAction();
        }
    }

    // 更新扫描计数和状态
    function updateScanCounts() {
        document.getElementById('normal-count').textContent = verifiedPackages.length;
        document.getElementById('danger-count').textContent = dangerPackages.length;
        document.getElementById('status-verified').textContent = verifiedPackages.length;

        // 更新普货/敏货面板
        const normalItems = verifiedPackages.filter(p => !dangerPackages.some(d => d.id === p.id)).length;
        const dangerItems = dangerPackages.length;
        document.getElementById('normal-count-display').textContent = normalItems;
        document.getElementById('danger-count-display').textContent = dangerItems;

        // 更新进度条
        const progress = totalPackages > 0 ? (verifiedPackages.length / totalPackages * 100) : 0;
        document.getElementById('scan-progress').style.width = progress + '%';

        // 更新扫描状态提示
        const statusDiv = document.getElementById('scan-status');
        const dangerSection = document.getElementById('danger-scan-section');

        if (verifiedPackages.length === 0) {
            statusDiv.style.background = 'rgba(217,119,6,0.1)';
            statusDiv.innerHTML = `
                <div style="font-weight: 600; color: #d97706; margin-bottom: 5px;">⚠️ 请先完成二次扫描</div>
                <div style="font-size: 13px; color: #d97706;">已扫描: <span id="status-verified">${verifiedPackages.length}</span> / ${totalPackages} 件</div>
            `;
            dangerSection.style.opacity = '0.5';
            dangerSection.style.pointerEvents = 'none';
        } else if (verifiedPackages.length < totalPackages) {
            statusDiv.style.background = 'rgba(217,119,6,0.1)';
            statusDiv.innerHTML = `
                <div style="font-weight: 600; color: #d97706; margin-bottom: 5px;">🔄 扫描中... 请继续扫描剩余包裹</div>
                <div style="font-size: 13px; color: #d97706;">已扫描: <span id="status-verified">${verifiedPackages.length}</span> / ${totalPackages} 件</div>
            `;
            dangerSection.style.opacity = '0.5';
            dangerSection.style.pointerEvents = 'none';
        } else {
            statusDiv.style.background = 'rgba(16,185,129,0.12)';
            statusDiv.innerHTML = `
                <div style="font-weight: 600; color: #16a34a; margin-bottom: 5px;">✅ 二次扫描完成！可以标记敏货或直接打包</div>
                <div style="font-size: 13px; color: #15803d;">已扫描: <span id="status-verified">${verifiedPackages.length}</span> / ${totalPackages} 件</div>
            `;
            dangerSection.style.opacity = '1';
            dangerSection.style.pointerEvents = 'auto';
            document.getElementById('danger-verify-input').style.background = 'var(--bg-card)';
        }
    }

    // 验证包裹并标记类型
    async function verifyPackage(tracking, type) {
        // 查找包裹
        const pkgItem = document.querySelector(`[data-tracking="${tracking}"]`);
        if (!pkgItem) {
            showToast('包裹不存在或不属于该客户', 'error');
            return;
        }

        const pkgId = parseInt(pkgItem.dataset.id);

        if (type === 'verify') {
            // 二次扫描
            if (verifiedPackages.some(p => p.id === pkgId)) {
                showToast('该包裹已二次扫描', 'info');
                return;
            }

            verifiedPackages.push({
                id: pkgId,
                tracking: tracking,
                seq: parseInt(pkgItem.dataset.seq)
            });

            // 更新UI
            updatePackageStatus(pkgId, 'verified');
            // 自动选中
            if (!selectedPackages.includes(pkgId)) {
                selectedPackages.push(pkgId);
                updatePackageSelectionUI(pkgId, true);
            }
            showToast(`✅ ${tracking} 已确认`, 'success');

        } else if (type === 'danger') {
            // 敏货标记
            if (!verifiedPackages.some(p => p.id === pkgId)) {
                showToast('请先对该包裹进行二次扫描', 'error');
                return;
            }

            if (dangerPackages.some(p => p.id === pkgId)) {
                // 取消敏货标记
                dangerPackages = dangerPackages.filter(p => p.id !== pkgId);
                updatePackageStatus(pkgId, 'verified');
                showToast(`已取消敏货标记: ${tracking}`, 'info');
            } else {
                dangerPackages.push({
                    id: pkgId,
                    tracking: tracking,
                    seq: parseInt(pkgItem.dataset.seq)
                });
                updatePackageStatus(pkgId, 'danger');
                showToast(`🔴 ${tracking} 已标记为敏货`, 'success');
            }
        }

        updateScanCounts();
        updateDistribution();
        saveScanState();  // 扫描后自动暂存
    }
    // 点击按钮标记/取消敏货
    async function markPackageAsDanger(pkgId) {
        const pkgItem = document.getElementById('pkg-' + pkgId);
        if (!pkgItem) return;

        const tracking = pkgItem.dataset.tracking;

        // 如果还没扫描过，先自动标记为已扫描
        if (!verifiedPackages.some(p => p.id === pkgId)) {
            verifiedPackages.push({
                id: pkgId,
                tracking: tracking,
                seq: parseInt(pkgItem.dataset.seq)
            });
            updatePackageStatus(pkgId, 'verified');
            // 自动选中
            if (!selectedPackages.includes(pkgId)) {
                selectedPackages.push(pkgId);
                updatePackageSelectionUI(pkgId, true);
            }
        }

        // 切换敏货标记
        if (dangerPackages.some(p => p.id === pkgId)) {
            dangerPackages = dangerPackages.filter(p => p.id !== pkgId);
            updatePackageStatus(pkgId, 'verified');
            showToast(`已取消敏货标记: ${tracking}`, 'info');
        } else {
            dangerPackages.push({
                id: pkgId,
                tracking: tracking,
                seq: parseInt(pkgItem.dataset.seq)
            });
            updatePackageStatus(pkgId, 'danger');
            showToast(`🔴 ${tracking} 已标记为敏货`, 'success');
        }

        updateScanCounts();
        updateDistribution();
        saveScanState();
    }

    function updatePackageStatus(pkgId, status) {
        const pkgItem = document.getElementById(`pkg-${pkgId}`);
        const badge = document.getElementById(`badge-${pkgId}`);

        pkgItem.dataset.status = status;
        pkgItem.classList.remove('verified', 'danger');
        badge.classList.remove('unverified', 'verified', 'danger');

        if (status === 'verified') {
            pkgItem.classList.add('verified');
            badge.classList.add('verified');
            badge.textContent = '✅已确认';
        } else if (status === 'danger') {
            pkgItem.classList.add('danger');
            badge.classList.add('danger');
            badge.textContent = '🔴敏货';
        } else {
            badge.classList.add('unverified');
            badge.textContent = '未扫描';
        }
    }

    // 切换包裹选择
    function togglePackageSelection(pkgId) {
        const idx = selectedPackages.indexOf(pkgId);
        if (idx > -1) {
            selectedPackages.splice(idx, 1);
            updatePackageSelectionUI(pkgId, false);
        } else {
            // 检查是否已二次扫描
            if (!verifiedPackages.some(p => p.id === pkgId)) {
                showToast('请先进行二次扫描', 'error');
                return;
            }
            selectedPackages.push(pkgId);
            updatePackageSelectionUI(pkgId, true);
        }
        updateDistribution();
    }

    function updatePackageSelectionUI(pkgId, selected) {
        const pkgItem = document.getElementById(`pkg-${pkgId}`);
        const checkBox = document.getElementById(`pkg-check-${pkgId}`);
        const selectBadge = document.getElementById(`select-badge-${pkgId}`);

        checkBox.checked = selected;
        if (selected) {
            pkgItem.classList.add('selected');
            selectBadge.style.display = 'inline-block';
        } else {
            pkgItem.classList.remove('selected');
            selectBadge.style.display = 'none';
        }
    }

    // 全选已扫描的包裹
    function selectAllVerified() {
        verifiedPackages.forEach(pkg => {
            if (!selectedPackages.includes(pkg.id)) {
                selectedPackages.push(pkg.id);
                updatePackageSelectionUI(pkg.id, true);
            }
        });
        showToast(`已选中 ${selectedPackages.length} 件包裹`, 'success');
        updateDistribution();
    }

    // 选择货物类型（已弃用，保留兼容）
    function selectNature(nature) {
        // 自动分拆模式下，类型由系统根据敏货标记自动判定
        updateDistribution();
    }

    // 标记全部为敏货
    async function markAllAsDanger() {
        if (verifiedPackages.length === 0) {
            showToast('请先进行二次扫描', 'error');
            return;
        }
        if (dangerPackages.length >= verifiedPackages.length) {
            showToast('所有已扫描包裹已标记为敏货', 'info');
            return;
        }

        for (const pkg of verifiedPackages) {
            if (!dangerPackages.some(p => p.id === pkg.id)) {
                dangerPackages.push(pkg);
                updatePackageStatus(pkg.id, 'danger');
            }
        }
        showToast(`已将 ${verifiedPackages.length} 件标记为敏货`, 'success');
        updateScanCounts();
        updateDistribution();
    }

    // 清空选择
    function clearSelection() {
        verifiedPackages = [];
        dangerPackages = [];
        selectedPackages = [];
        updateScanCounts();
        updateDistribution();

        // 重置所有包裹UI
        document.querySelectorAll('.package-item').forEach(item => {
            const pkgId = parseInt(item.dataset.id);
            updatePackageStatus(pkgId, 'unverified');
            updatePackageSelectionUI(pkgId, false);
        });

        showToast('已清空选择', 'info');
    }

    // 更新分配预览 - 同时显示普货和敏货的分配
    function updateDistribution() {
        const normalCount = parseInt(document.getElementById('normal-pkg-count').value) || 1;
        const dangerCount = parseInt(document.getElementById('danger-pkg-count').value) || 1;
        const preview = document.getElementById('distribution-preview');

        // 普货：已扫描但未标记敏货的
        const normalItems = verifiedPackages.filter(p => !dangerPackages.some(d => d.id === p.id));
        // 敏货：已标记的
        const dangerItems = dangerPackages;

        if (verifiedPackages.length === 0) {
            preview.innerHTML = `<span style="color: #dc2626;">⚠️ 请先完成二次扫描</span>`;
            document.getElementById('create-btn').disabled = true;
            return;
        }

        // 使用手动选择的包裹进行分配
        let selectedNormal = selectedPackages
            .map(id => normalItems.find(p => p.id === id))
            .filter(Boolean);
        let selectedDanger = selectedPackages
            .map(id => dangerItems.find(p => p.id === id))
            .filter(Boolean);

        // 如果没有手动选择，用全部已扫描的分组
        if (selectedPackages.length === 0) {
            selectedNormal = normalItems;
            selectedDanger = dangerItems;
        }

        const totalSelected = selectedNormal.length + selectedDanger.length;
        if (totalSelected === 0) {
            preview.innerHTML = `<span style="color: #d97706;">请在左侧选择要打包的包裹（${verifiedPackages.length} 件已扫描）</span>`;
            document.getElementById('create-btn').disabled = true;
            return;
        }

        let html = `<div style="margin-bottom: 10px;">已选 <strong style="color:#06b6d4">${totalSelected}</strong> 件</div>`;

        // 普货分配
        if (selectedNormal.length > 0 && normalCount > 0) {
            const base = Math.floor(selectedNormal.length / normalCount);
            const remainder = selectedNormal.length % normalCount;
            html += `<div style="margin-top: 8px; padding: 8px; background: rgba(16,185,129,0.06); border-radius: 6px; border-left: 3px solid #16a34a;">`;
            html += `<div style="font-weight: 600; color: #16a34a; margin-bottom: 5px;">📦 普货：${selectedNormal.length} 件 → ${normalCount} 包</div>`;
            for (let i = 0; i < normalCount; i++) {
                const n = base + (i < remainder ? 1 : 0);
                html += `<div style="font-size: 12px; color: #15803d;">包 ${normalCount}-${i + 1}: ${n} 件</div>`;
            }
            html += `</div>`;
        }

        // 敏货分配
        if (selectedDanger.length > 0 && dangerCount > 0) {
            const base = Math.floor(selectedDanger.length / dangerCount);
            const remainder = selectedDanger.length % dangerCount;
            html += `<div style="margin-top: 8px; padding: 8px; background: rgba(239,68,68,0.06); border-radius: 6px; border-left: 3px solid #dc2626;">`;
            html += `<div style="font-weight: 600; color: #dc2626; margin-bottom: 5px;">🔴 敏货：${selectedDanger.length} 件 → ${dangerCount} 包</div>`;
            for (let i = 0; i < dangerCount; i++) {
                const n = base + (i < remainder ? 1 : 0);
                html += `<div style="font-size: 12px; color: #b91c1c;">包 ${dangerCount}-${i + 1}: ${n} 件</div>`;
            }
            html += `</div>`;
        }

        // 没有货品但选了count
        if (selectedNormal.length === 0 && normalCount > 0 && normalItems.length === 0) {
            html += `<div style="margin-top: 8px; color: var(--text-muted); font-size: 12px;">📦 普货：暂无可打包的普货</div>`;
        }
        if (selectedDanger.length === 0 && dangerCount > 0 && dangerItems.length === 0) {
            html += `<div style="margin-top: 8px; color: var(--text-muted); font-size: 12px;">🔴 敏货：暂无可打包的敏货</div>`;
        }

        preview.innerHTML = html;

        // 有效的打包判断
        const hasNormal = selectedNormal.length > 0 && normalCount > 0 && selectedNormal.length >= normalCount;
        const hasDanger = selectedDanger.length > 0 && dangerCount > 0 && selectedDanger.length >= dangerCount;
        const canPack = hasNormal || hasDanger;
        
        document.getElementById('create-btn').disabled = !canPack;
        if (!canPack) {
            const reasons = [];
            if (selectedNormal.length > 0 && selectedNormal.length < normalCount) reasons.push('普货数量不足');
            if (selectedDanger.length > 0 && selectedDanger.length < dangerCount) reasons.push('敏货数量不足');
            if (totalSelected === 0) reasons.push('未选择包裹');
            document.getElementById('create-btn').textContent = reasons.join('；') || '无法打包';
        } else {
            const parts = [];
            if (hasNormal) parts.push(`${selectedNormal.length}件普货→${normalCount}包`);
            if (hasDanger) parts.push(`${selectedDanger.length}件敏货→${dangerCount}包`);
            document.getElementById('create-btn').textContent = `✅ 确认打包（${parts.join('，')}）`;
        }
    }

    // 单包裹拆分打包 - 同一包裹打成普货+敏货两包
    async function createSplitPacked() {
        const batchId = document.getElementById('batch-select').value || null;
        const pkgItem = document.querySelector('.package-item');
        if (!pkgItem) { showToast('未找到包裹', 'error'); return; }
        const pkgId = parseInt(pkgItem.dataset.id);
        const tracking = pkgItem.dataset.tracking;

        confirmAction('拆分打包确认',
            '将单号 <strong>' + tracking + '</strong> 拆分为：<br>' +
            '<span style="color:#16a34a;">📦 普货 1件 → 1包</span><br>' +
            '<span style="color:#dc2626;">🔴 敏货 1件 → 1包</span><br><br>' +
            '<span style="font-size:13px;color:var(--text-muted);">共创建2个打包记录</span>',
            async function() {
                lockAction();
                try {
                    const allPackages = [
                        { serial: '1-1', total: 1, nature: '普', package_ids: [pkgId] },
                        { serial: '2-2', total: 1, nature: '敏', package_ids: [pkgId] }
                    ];
                    const result = await apiRequest('/api/create_packed_batch', {
                        method: 'POST',
                        body: JSON.stringify({
                            customer_id: 0,
                            packages: allPackages,
                            batch_id: batchId
                        })
                    });

                    const packedIds = result.data && result.data.packed_ids ? result.data.packed_ids : [];
                    if (packedIds.length === 0) {
                        showToast('创建打包记录失败', 'error');
                        return;
                    }

                    // 展示详情弹窗（与正常打包流程一致）
                    document.getElementById('packed-ids').value = JSON.stringify(packedIds);
                    document.getElementById('package-serials').value = JSON.stringify(['普货包 1-1', '敏货包 2-2']);
                    document.getElementById('label-preview').innerHTML = `
                        <strong>0</strong><br>
                        <span style="display:inline-block;background:#16a34a;color:#fff;padding:5px 12px;border-radius:6px;margin:3px;">📦 普货 1-1</span>
                        <span style="display:inline-block;background:#dc2626;color:#fff;padding:5px 12px;border-radius:6px;margin:3px;">🔴 敏货 2-2</span>
                    `;
                    generateMultiPackageInputs(packedIds, ['普货包 1-1', '敏货包 2-2'], ['普', '敏']);
                    document.getElementById('detail-modal').classList.add('active');

                } catch (e) {
                    showToast(e.message || '拆分打包失败', 'error');
                } finally {
                    unlockAction();
                }
            });
    }

    // 创建打包记录 - 普货敏货自动分拆，同步打包，禁止混装
    async function createPacked() {
        if (isActionLocked()) return;

        // 拆分模式特殊处理
        if (splitMode && totalPackages === 1) {
            return createSplitPacked();
        }

        // ====== 检查是否所有包裹已完成二次扫描 ======
        const scannedCount = verifiedPackages.length;
        if (scannedCount < totalPackages) {
            const unscanned = totalPackages - scannedCount;
            showToast(`还有 ${unscanned} 件包裹未完成二次扫描，请先扫完全部包裹再打包`, 'error');

            // 高亮提示区域
            const statusDiv = document.getElementById('scan-status');
            statusDiv.style.background = 'rgba(220,38,38,0.12)';
            statusDiv.style.border = '2px solid #dc2626';
            statusDiv.style.animation = 'none';
            statusDiv.offsetHeight; // reflow
            statusDiv.style.animation = 'shake 0.5s ease-in-out';
            document.getElementById('status-verified').style.color = '#dc2626';
            document.getElementById('status-verified').style.fontWeight = 'bold';

            // 滚动到扫描区
            document.querySelector('[style*="linear-gradient(135deg, rgba(16,185,129"]').scrollIntoView({ behavior: 'smooth', block: 'center' });
            return;
        }
        // 恢复扫描状态样式
        document.getElementById('scan-status').style.background = 'rgba(217,119,6,0.1)';
        document.getElementById('scan-status').style.border = '1px solid var(--warning)';
        document.getElementById('status-verified').style.color = '#d97706';
        document.getElementById('status-verified').style.fontWeight = 'normal';
        const normalCount = parseInt(document.getElementById('normal-pkg-count').value) || 1;
        const dangerCount = parseInt(document.getElementById('danger-pkg-count').value) || 1;
        const batchId = document.getElementById('batch-select').value || null;

        // 普货：已扫描但未标记敏货的
        const normalItems = verifiedPackages.filter(p => !dangerPackages.some(d => d.id === p.id));
        // 敏货：已标记的
        const dangerItems = dangerPackages;

        // 使用手动选择的包裹
        let selectedNormal = selectedPackages.length > 0
            ? selectedPackages.map(id => normalItems.find(p => p.id === id)).filter(Boolean)
            : normalItems;
        let selectedDanger = selectedPackages.length > 0
            ? selectedPackages.map(id => dangerItems.find(p => p.id === id)).filter(Boolean)
            : dangerItems;

        const hasNormal = selectedNormal.length > 0 && selectedNormal.length >= normalCount;
        const hasDanger = selectedDanger.length > 0 && selectedDanger.length >= dangerCount;

        if (!hasNormal && !hasDanger) {
            showToast('请选择要打包的包裹', 'error');
            return;
        }

        // 构建所有打包数据（普货 + 敏货）
        const allPackages = [];
        const allSerials = [];
        const natureLabels = [];

        // 普货打包
        if (hasNormal) {
            const base = Math.floor(selectedNormal.length / normalCount);
            const remainder = selectedNormal.length % normalCount;
            let idx = 0;
            for (let i = 0; i < normalCount; i++) {
                const n = base + (i < remainder ? 1 : 0);
                const pkgIds = [];
                for (let j = 0; j < n && idx < selectedNormal.length; j++) {
                    pkgIds.push(selectedNormal[idx].id);
                    idx++;
                }
                const serial = `${normalCount}-${i + 1}`;
                allSerials.push(serial);
                natureLabels.push('普');
                allPackages.push({
                    serial: serial,
                    total: normalCount,
                    nature: '普',
                    package_ids: pkgIds
                });
            }
        }

        // 敏货打包（禁止混装，独立创建）
        if (hasDanger) {
            const base = Math.floor(selectedDanger.length / dangerCount);
            const remainder = selectedDanger.length % dangerCount;
            let idx = 0;
            for (let i = 0; i < dangerCount; i++) {
                const n = base + (i < remainder ? 1 : 0);
                const pkgIds = [];
                for (let j = 0; j < n && idx < selectedDanger.length; j++) {
                    pkgIds.push(selectedDanger[idx].id);
                    idx++;
                }
                const serial = `${dangerCount}-${i + 1}`;
                allSerials.push(serial);
                natureLabels.push('敏');
                allPackages.push({
                    serial: serial,
                    total: dangerCount,
                    nature: '敏',
                    package_ids: pkgIds
                });
            }
        }

        // 构建确认消息
        const parts = [];
        if (hasNormal) parts.push(`<span style="color:#16a34a;">📦 普货 ${selectedNormal.length}件 → ${normalCount}包</span>`);
        if (hasDanger) parts.push(`<span style="color:#dc2626;">🔴 敏货 ${selectedDanger.length}件 → ${dangerCount}包</span>`);
        const confirmMsg = `即将同步创建：<br>${parts.join('<br>')}<br><br><span style="font-size:13px;color:var(--text-muted);">普货与敏货分开打包，绝不混装</span>`;

        // ====== 检测该客户在此批次是否已有打包记录 ======
        // 保存本次打包参数，供弹窗回调使用
        _pendingPackPayload = { allPackages, allSerials, natureLabels, batchId, confirmMsg };

        let checkUrl = `/api/packed/check_existing?customer_id=0`;
        if (batchId) checkUrl += `&batch_id=${batchId}`;

        lockAction();
        try {
            const checkResult = await apiRequest(checkUrl, { method: 'GET' });
            if (checkResult.data && checkResult.data.exists) {
                // 有现有记录 → 弹出选择弹窗
                showExistingPackedModal(checkResult.data.records);
                return;
            }
        } catch (e) {
            // 检测失败不阻断流程，直接走正常打包
        } finally {
            unlockAction();
        }

        // 无现有记录，走原有流程
        doConfirmAndPack(_pendingPackPayload);
    }

    // 显示现有打包记录选择弹窗
    function showExistingPackedModal(records) {
        _selectedAppendTarget = null;

        const list = document.getElementById('existing-records-list');
        let html = '';
        records.forEach(r => {
            const natureColor = r.nature === '敏' ? '#dc2626' : '#16a34a';
            const cartonedTag = r.is_cartoned
                ? '<span style="font-size:11px;background:#94a3b8;color:#fff;padding:2px 6px;border-radius:3px;margin-left:6px;">已合箱</span>'
                : '';
            const trackingPreview = r.tracking_numbers.slice(0, 3).join('、') + (r.tracking_numbers.length > 3 ? ` 等${r.tracking_numbers.length}件` : '');
            html += `
            <div class="existing-record-item" id="erec-${r.id}" data-id="${r.id}"
                style="padding:12px 15px;border:2px solid var(--border);border-radius:10px;margin-bottom:8px;cursor:pointer;transition:all 0.2s;background:var(--bg-card);"
                onclick="selectAppendTarget(${r.id}, '${r.serial_label}', '${r.nature}', ${r.item_count})"
                onmouseenter="this.style.borderColor='var(--accent)'"
                onmouseleave="if(_selectedAppendTarget!=${r.id}) this.style.borderColor='var(--border)'">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <div style="display:flex;align-items:center;gap:10px;">
                        <span style="font-size:16px;font-weight:700;color:var(--accent-hover);">${r.serial_label}</span>
                        <span style="font-size:12px;background:${natureColor};color:#fff;padding:2px 8px;border-radius:4px;">${r.nature}货</span>
                        ${cartonedTag}
                    </div>
                    <span style="font-size:13px;color:var(--text-secondary);">已含 ${r.item_count} 件</span>
                </div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:5px;">
                    ${r.contents_description ? `📝 ${r.contents_description}` : '（未填写内容物）'}
                </div>
                <div style="font-size:11px;color:var(--text-muted);margin-top:4px;">
                    📦 ${trackingPreview}
                </div>
            </div>`;
        });
        list.innerHTML = html || '<div style="color:var(--text-muted);text-align:center;padding:20px;">暂无记录</div>';

        document.getElementById('append-target-area').style.display = 'none';
        document.getElementById('confirm-append-btn').style.display = 'none';
        document.getElementById('existing-packed-modal').classList.add('active');
    }

    // 选中一条打包记录作为追加目标
    function selectAppendTarget(id, serial, nature, itemCount) {
        // 取消上一个选中
        if (_selectedAppendTarget) {
            const prev = document.getElementById(`erec-${_selectedAppendTarget}`);
            if (prev) { prev.style.borderColor = 'var(--border)'; prev.style.background = 'var(--bg-card)'; }
        }
        _selectedAppendTarget = id;

        const el = document.getElementById(`erec-${id}`);
        if (el) { el.style.borderColor = 'var(--success)'; el.style.background = 'rgba(16,185,129,0.06)'; }

        const natureColor = nature === '敏' ? '#dc2626' : '#16a34a';
        document.getElementById('append-target-info').innerHTML =
            `<strong style="color:${natureColor};">${serial}</strong>（${nature}货，已含 ${itemCount} 件）`;
        document.getElementById('append-target-area').style.display = 'block';
        document.getElementById('confirm-append-btn').style.display = 'inline-flex';
    }

    function closeExistingModal() {
        document.getElementById('existing-packed-modal').classList.remove('active');
        _pendingPackPayload = null;
        _selectedAppendTarget = null;
    }

    // 选择"重新打包" - 走原流程
    function proceedRepack() {
        document.getElementById('existing-packed-modal').classList.remove('active');
        if (_pendingPackPayload) {
            doConfirmAndPack(_pendingPackPayload);
        }
    }

    // 选择"追加到所选包裹"
    async function proceedAppend() {
        if (!_selectedAppendTarget) {
            showToast('请先选择要追加到的打包记录', 'error');
            return;
        }
        if (!_pendingPackPayload) return;

        document.getElementById('existing-packed-modal').classList.remove('active');

        // 收集所有选中的入库包裹 id
        const { allPackages } = _pendingPackPayload;
        const allPkgIds = [];
        allPackages.forEach(p => allPkgIds.push(...p.package_ids));

        lockAction();
        try {
            await apiRequest(`/api/packed/${_selectedAppendTarget}/append_items`, {
                method: 'POST',
                body: JSON.stringify({ package_ids: allPkgIds })
            });
            showToast(`✅ 已成功将 ${allPkgIds.length} 件追加到打包记录`, 'success');
            setTimeout(() => window.location.reload(), 1200);
        } catch (e) {
            showToast(e.message || '追加失败', 'error');
        } finally {
            unlockAction();
        }
    }

    // 原有确认+打包逻辑（抽取为独立函数）
    function doConfirmAndPack(payload) {
        const { allPackages, allSerials, natureLabels, batchId, confirmMsg } = payload;

        confirmAction('确认打包', confirmMsg, async () => {
            lockAction();
            try {
                const result = await apiRequest('/api/create_packed_batch', {
                    method: 'POST',
                    body: JSON.stringify({
                        customer_id: 0,
                        packages: allPackages,
                        batch_id: batchId
                    })
                });

                const packedIds = result.data && result.data.packed_ids ? result.data.packed_ids : [];

                if (packedIds.length === 0) {
                    showToast('创建打包记录失败：未返回有效的打包ID', 'error');
                    return;
                }

                // 打包已提交，清除扫描暂存
                clearScanState();

                // 显示发货明细弹窗
                document.getElementById('packed-ids').value = JSON.stringify(packedIds);
                document.getElementById('package-serials').value = JSON.stringify(allSerials);

                // 生成包裹预览（区分颜色）
                const preview = allSerials.map((s, i) => {
                    const color = natureLabels[i] === '敏' ? '#dc2626' : '#16a34a';
                    const emoji = natureLabels[i] === '敏' ? '🔴' : '📦';
                    return `<span style="display:inline-block;background:${color};color:#fff;padding:5px 12px;border-radius:6px;margin:3px;font-size:14px;">${emoji} ${s}</span>`;
                }).join('');
                document.getElementById('label-preview').innerHTML = `
                    <strong>0</strong><br>
                    ${preview}
                `;

                // 动态生成多包裹输入区域（含类型信息）
                generateMultiPackageInputs(packedIds, allSerials, natureLabels);

                document.getElementById('detail-modal').classList.add('active');

            } catch (e) {
                showToast(e.message || '创建失败', 'error');
            } finally {
                unlockAction();
            }
        });
    }

    // 动态生成多包裹输入区域
    function generateMultiPackageInputs(packedIds, serials, natureLabels) {
        const container = document.getElementById('multi-packages-input');
        const count = packedIds.length;
        natureLabels = natureLabels || [];

        // 只填内容物描述，重量尺寸后续在打包记录中修改
        let html = '';
        for (let i = 0; i < count; i++) {
            const bgColor = i % 2 === 0 ? 'var(--bg-input)' : 'var(--bg-card)';
            const nature = natureLabels[i] || '普';
            const natureColor = nature === '敏' ? '#dc2626' : '#16a34a';
            const natureText = nature === '敏' ? '敏货' : '普货';

            html += '<div style="padding: 15px; background: ' + bgColor + '; border: 1px solid rgba(148,163,184,0.12); border-radius: 10px; margin-bottom: 12px;">' +
                '<div style="font-weight: 600; color: var(--accent-hover); margin-bottom: 12px; display: flex; align-items: center; gap: 10px;">' +
                    '📦 包裹 ' + serials[i] +
                    '<span style="font-size: 12px; padding: 2px 8px; background: ' + natureColor + '; color: #fff; border-radius: 4px; font-weight: normal;">' + natureText + '</span>' +
                '</div>' +
                '<div class="form-group" style="margin-bottom:0;">' +
                    '<label style="font-size: 13px;">📝 内容物描述 <span style="color:#dc2626;">*</span></label>' +
                    '<textarea id="pkg-' + i + '-contents" rows="2" placeholder="必填：描述包裹内的商品内容（如：衣服5件、鞋子2双）" style="font-size:14px;"></textarea>' +
                '</div>' +
            '</div>';
        }
        container.innerHTML = html;
    }

    async function closeDetailModal() {
        // 需要确认才回退
        const packedIds = JSON.parse(document.getElementById('packed-ids').value);
        confirmAction('取消打包', `确定要取消吗？<br><br><span style="color:#dc2626;">刚创建的 <strong>${packedIds.length}</strong> 个打包记录将被<strong>永久删除</strong>，包裹恢复为待打包状态。</span>`, async () => {
            try {
                for (const pid of packedIds) {
                    await apiRequest('/api/packed/records/rollback/' + pid, { method: 'POST' });
                }
            } catch (e) {
                // 回退失败不阻塞
            }
            document.getElementById('detail-modal').classList.remove('active');
            window.location.reload();
        });
    }

    function copyFirstToAll() {
        const packedIds = JSON.parse(document.getElementById('packed-ids').value);
        if (packedIds.length <= 1) {
            showToast('只有一个包裹，无需复制', 'info');
            return;
        }

        const contents = document.getElementById('pkg-0-contents').value;
        for (let i = 1; i < packedIds.length; i++) {
            document.getElementById('pkg-' + i + '-contents').value = contents;
        }
        showToast('已复制到所有包裹', 'success');
    }

    // 打印队列管理
    let printQueue = [];
    let printSerials = [];
    let printIndex = 0;
    let printCancelled = false;
    let printedFlags = [];

    async function saveAndPrint() {
        const packedIds = JSON.parse(document.getElementById('packed-ids').value);
        const count = packedIds.length;

        // 验证：内容物描述必填
        for (let i = 0; i < count; i++) {
            const contents = document.getElementById('pkg-' + i + '-contents').value.trim();
            if (!contents) {
                showToast('包裹 ' + (i + 1) + ' 请填写内容物描述', 'error');
                return;
            }
        }

        try {
            // 分别更新每个打包记录的内容物
            for (let i = 0; i < count; i++) {
                let contents = document.getElementById('pkg-' + i + '-contents').value.trim();
                const uniformContents = document.getElementById('contents-description').value.trim();
                if (!contents && uniformContents) {
                    contents = uniformContents;
                }

                await apiRequest('/api/packed/' + packedIds[i] + '/update', {
                    method: 'POST',
                    body: JSON.stringify({ description: contents })
                });
            }

            // 保存成功，初始化打印队列
            const serials = JSON.parse(document.getElementById('package-serials').value);
            document.getElementById('detail-modal').classList.remove('active');

            printQueue = packedIds;
            printSerials = serials;
            printIndex = 0;
            printCancelled = false;
            printedFlags = new Array(packedIds.length).fill(false);

            // 显示打印队列弹窗
            document.getElementById('print-queue-count').textContent = `共 ${count} 张标签`;
            document.getElementById('print-queue-modal').classList.add('active');

            updatePrintQueueUI();

        } catch (e) {
            showToast(e.message || '保存失败', 'error');
        }
    }

    function updatePrintQueueUI() {
        if (printCancelled) return;

        const total = printQueue.length;

        // 更新进度
        document.getElementById('print-current').textContent = `${printIndex + 1} / ${total}`;
        document.getElementById('print-progress-bar').style.width = `${(printIndex / total) * 100}%`;
        document.getElementById('print-current-label').textContent = `当前标签: ${printSerials[printIndex]}`;

        // 控制按钮状态
        const finishArea = document.getElementById('print-finish-area');
        const actionButtons = document.getElementById('print-action-buttons');

        if (printIndex >= total) {
            // 全部处理完毕
            actionButtons.style.display = 'none';
            finishArea.style.display = 'block';
            document.getElementById('print-current-label').textContent = '所有标签已处理完毕';
            document.getElementById('print-current-label').style.background = 'rgba(16,185,129,0.12)';
            document.getElementById('print-current-label').style.color = '#16a34a';
        } else {
            actionButtons.style.display = 'flex';
            finishArea.style.display = 'none';
            document.getElementById('print-current-label').style.background = 'rgba(6,182,212,0.06)';
            document.getElementById('print-current-label').style.color = 'var(--text-primary)';
        }
    }

    function printCurrentLabel() {
        if (printCancelled || printIndex >= printQueue.length) return;
        const packedId = printQueue[printIndex];
        window.open(
            `/print_packed_label/${packedId}`,
            `print_${packedId}_${Date.now()}`,
            'width=420,height=500,toolbar=0,scrollbars=0,status=0'
        );
        printedFlags[printIndex] = true;
        showToast(`已打开标签 ${printSerials[printIndex]} 的打印窗口`, 'success');
    }

    function pdfCurrentLabel() {
        if (printCancelled || printIndex >= printQueue.length) return;
        const packedId = printQueue[printIndex];
        // 直接触发PDF下载
        const link = document.createElement('a');
        link.href = `/pdf/packed_label/${packedId}`;
        link.download = `packed_label_${printSerials[printIndex]}.pdf`;
        link.click();
        printedFlags[printIndex] = true;
        showToast(`正在下载标签 ${printSerials[printIndex]} 的PDF`, 'success');
    }

    function nextPrintLabel() {
        if (printCancelled || printIndex >= printQueue.length) return;
        printIndex++;
        updatePrintQueueUI();
    }

    function skipPrint() {
        if (printCancelled || printIndex >= printQueue.length) return;
        printIndex++;
        updatePrintQueueUI();
    }

    async function cancelPrintQueue() {
        printCancelled = true;
        document.getElementById('print-queue-modal').classList.remove('active');
        showToast('已取消打印', 'info');
        // 检查是否有未打包包裹需要退回
        try {
            const checkResult = await apiRequest(`/api/pack/0/remaining_count`, { method: 'GET' });
            const remainingCount = checkResult.data && checkResult.data.count || 0;
            if (remainingCount > 0) {
                setTimeout(() => showReturnConfirm(remainingCount), 500);
                return;
            }
        } catch (e) {}
        setTimeout(() => window.location.reload(), 500);
    }

    async function finishPrintQueue() {
        document.getElementById('print-queue-modal').classList.remove('active');
        const printedCount = printedFlags.filter(f => f).length;
        showToast(`✅ 已完成 ${printedCount}/${printQueue.length} 张标签打印`, 'success');

        // 检查是否有未打包的包裹需要退回
        try {
            const checkResult = await apiRequest(`/api/pack/0/remaining_count`, { method: 'GET' });
            const remainingCount = checkResult.data && checkResult.data.count || 0;
            if (remainingCount > 0) {
                // 有剩余包裹未打包，弹窗确认是否退回
                showReturnConfirm(remainingCount);
                return;
            }
        } catch (e) {
            // 检查失败不阻塞，直接刷新
        }
        window.location.reload();
    }

    function showReturnConfirm(count) {
        // 创建确认弹窗
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center;';
        overlay.innerHTML = `
            <div style="background:var(--bg-card,#fff);border-radius:16px;padding:30px;max-width:480px;width:90%;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,0.3);">
                <div style="font-size:48px;margin-bottom:15px;">⚠️</div>
                <div style="font-size:18px;font-weight:700;color:var(--text-primary,#1e293b);margin-bottom:10px;">还有包裹未打包</div>
                <div style="font-size:15px;color:var(--text-secondary,#64748b);margin-bottom:8px;line-height:1.6;">
                    检测到 <strong style="color:#d97706;font-size:18px;">${count}</strong> 件包裹<strong>未被打包</strong>
                </div>
                <div style="font-size:13px;color:#ef4444;margin-bottom:20px;padding:10px;background:rgba(239,68,68,0.06);border-radius:8px;border:1px solid rgba(239,68,68,0.15);">
                    ⚠️ 建议退回已入库状态，防止遗漏（如客户只发敏货不发普货）
                </div>
                <div style="display:flex;gap:12px;justify-content:center;">
                    <button id="btn-return-yes" style="padding:12px 28px;background:#d97706;color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;">🔄 确认退回已入库</button>
                    <button id="btn-return-no" style="padding:12px 28px;background:var(--border,#e2e8f0);color:var(--text-secondary,#64748b);border:none;border-radius:10px;font-size:15px;cursor:pointer;">暂不处理</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        document.getElementById('btn-return-yes').onclick = async function() {
            overlay.remove();
            lockAction();
            try {
                await apiRequest('/api/pack/0/return_remaining', { method: 'POST' });
                showToast(`✅ 已将 ${count} 件包裹退回已入库状态`, 'success');
            } catch (e) {
                showToast(e.message || '退回失败', 'error');
            } finally {
                unlockAction();
                setTimeout(() => window.location.reload(), 800);
            }
        };

        document.getElementById('btn-return-no').onclick = function() {
            overlay.remove();
            window.location.reload();
        };
    }

    // 初始化
    updateScanCounts();
    updateDistribution();
    selectNature('普'); // 默认选择普货

    // ========== 漏扫录入（打包操作界面内） ==========
    function showQuickAddInPack() {
        document.getElementById('quick-add-inline').style.display = 'block';
        document.getElementById('quick-add-btn').style.display = 'none';
        setTimeout(() => document.getElementById('quick-add-tracking').focus(), 100);
    }

    function hideQuickAddInPack() {
        document.getElementById('quick-add-inline').style.display = 'none';
        document.getElementById('quick-add-btn').style.display = '';
        document.getElementById('quick-add-tracking').value = '';
    }

    async function submitQuickAddInPack() {
        const input = document.getElementById('quick-add-tracking');
        const tracking = input.value.trim();
        if (!tracking) { showToast('请输入快递单号', 'warning'); input.focus(); return; }
        if (isActionLocked()) return;
        lockAction();
        try {
            const result = await apiRequest('/api/inbound/quick_add', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ customer_id: 0, tracking_number: tracking })
            });
            addPackageToUI(result.id, result.tracking_number || tracking, result.sequence_no || 0);
            input.value = '';
            showToast(result.message || '录入成功', 'success');
            // 保持输入框打开以便连续录入，自动聚焦
            setTimeout(() => input.focus(), 200);
        } catch (e) {
            showToast(e.message || '录入失败', 'error');
            input.focus();
        } finally {
            unlockAction();
        }
    }

    function addPackageToUI(id, tracking, seq) {
        totalPackages++;

        const container = document.getElementById('package-list');
        const div = document.createElement('div');
        div.className = 'package-item';
        div.id = 'pkg-' + id;
        div.setAttribute('data-id', id);
        div.setAttribute('data-tracking', tracking);
        div.setAttribute('data-seq', seq);
        div.setAttribute('data-is-danger', 'false');
        div.setAttribute('data-status', 'unverified');
        div.setAttribute('onclick', 'togglePackageSelection(' + id + ')');
        div.style.cssText = 'padding: 15px; border: 2px solid var(--border); border-radius: 10px; margin-bottom: 10px; transition: all 0.3s; background: var(--bg-card); cursor: pointer;';

        div.innerHTML =
            '<div style="display: flex; justify-content: space-between; align-items: center;">' +
                '<div style="display: flex; align-items: center; gap: 15px;">' +
                    '<input type="checkbox" id="pkg-check-' + id + '" onclick="event.stopPropagation(); togglePackageSelection(' + id + ')" style="width: 20px; height: 20px; cursor: pointer;">' +
                    '<div>' +
                        '<div style="font-weight: 600; font-size: 15px;">' + tracking + '</div>' +
                        '<div style="font-size: 12px; color: var(--text-secondary);">序号: ' + seq + '</div>' +
                    '</div>' +
                '</div>' +
                '<div style="display: flex; gap: 10px; align-items: center;">' +
                    '<button class="btn-mark-danger" id="mark-danger-btn-' + id + '" onclick="event.stopPropagation(); markPackageAsDanger(' + id + ')" title="标记为敏货" style="padding: 3px 8px; border: 1px solid #f87171; border-radius: 5px; background: transparent; color: #dc2626; cursor: pointer; font-size: 11px; font-weight: 600;">🔴敏</button>' +
                    '<span class="verify-badge" id="badge-' + id + '" style="padding: 4px 12px; border-radius: 6px; font-size: 12px; font-weight: 600; background: var(--border); color: var(--text-secondary);">未扫描</span>' +
                    '<span id="select-badge-' + id + '" style="display: none; padding: 4px 12px; border-radius: 6px; font-size: 12px; font-weight: 600; background: var(--accent); color: #fff;">已选</span>' +
                '</div>' +
            '</div>';

        container.appendChild(div);

        // 高亮新包裹 2 秒
        div.style.border = '3px dashed var(--warning)';
        div.style.background = 'rgba(245,158,11,0.08)';
        setTimeout(function() {
            div.style.border = '2px solid var(--border)';
            div.style.background = 'var(--bg-card)';
        }, 2000);

        // 更新所有计数
        updateScanCounts();
        updateDistribution();

        // 滚动到新包裹
        div.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }