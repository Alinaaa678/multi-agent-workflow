# 设备借用管理（Demo）

- spec_version: S2-v0.1
- contract_version: C2-v0.1
- project_id: lab-equipment-loan

## 需求

### EQ-01 设备登记
- 描述：管理员登记实验室设备，包含名称、编号、状态。
- 优先级：must
- 验收：编号唯一时登记成功
- 验收：编号重复时返回 DUPLICATE_EQUIPMENT
- 约束：编号由管理员提供，不做自动生成

### EQ-02 借用申请
- 描述：校内用户对可用设备提交借用申请，写明借用起止时间。
- 优先级：must
- 验收：起止时间合法时创建成功
- 验收：结束时间早于开始时间时拒绝
- 约束：同一设备同一时段不允许重复占用

### EQ-03 归还登记
- 描述：管理员登记归还，设备状态回到可用。
- 优先级：should
- 验收：归还后设备状态为 available
- 验收：未借出的设备不允许归还

## 设计条目

### DS-EQ-REG-01 设备登记
- 模块：设备管理
- 需求：EQ-01
- 契约：register_equipment
- 测试：TC-EQ-REG-01
- 规则：设备编号唯一且大小写不敏感比较。

### DS-EQ-LOAN-01 借用申请
- 模块：借用管理
- 需求：EQ-02
- 契约：create_loan
- 测试：TC-EQ-LOAN-01
- 规则：借用时间段不得与既有有效借用重叠。

### DS-EQ-RETURN-01 归还登记
- 模块：借用管理
- 需求：EQ-03
- 契约：return_loan
- 测试：TC-EQ-RETURN-01
- 规则：仅 borrowed 状态的设备可归还。

## 契约

### register_equipment
- method: POST
- path: /api/v1/equipment
- authenticated: true
- required: code, name
- status: 201
- data: EquipmentView
- error: DUPLICATE_EQUIPMENT=409

### create_loan
- method: POST
- path: /api/v1/loans
- authenticated: true
- required: equipment_id, start_at, end_at
- status: 201
- data: LoanView
- error: INVALID_FIELD=400

### return_loan
- method: POST
- path: /api/v1/loans/{id}/return
- authenticated: true
- required: 
- status: 200
- data: LoanView
- error: INVALID_STATE=409
