# Example Contracts

This directory contains example smart contracts for testing the MCP Smart Contract Auditor.

## SimpleToken.sol

A basic ERC20-like token contract that demonstrates:
- Token transfers
- Approval mechanism
- Balance tracking
- Safe arithmetic (using Solidity 0.8+)

This is a relatively safe contract that should pass most security checks.

## Usage

To audit these contracts, use the MCP server tools:

```bash
# Start the MCP server (Python package — not npm)
python3 -m farofino_mcp
# or, if installed as a console script:
farofino-mcp

# Then use the tools through your MCP client:
# 0. Run every available analyzer at once (recommended)
audit_project with contract_path="examples/SimpleToken.sol"

# 1. Read the contract
read_contract with contract_path="examples/SimpleToken.sol"

# 2. Run pattern analysis
pattern_analysis with contract_path="examples/SimpleToken.sol"

# 3. Run Slither (accepts a file OR a project root directory)
slither_audit with contract_path="examples/SimpleToken.sol"

# 4. Run Aderyn (accepts a file OR a project root directory)
aderyn_audit with contract_path="examples/SimpleToken.sol"
```

Prefer pointing `audit_project` / `slither_audit` / `aderyn_audit` at a **project
root** (the folder with `foundry.toml` or `hardhat.config.*`) for contracts that
import other files (e.g. OpenZeppelin) so imports and remappings resolve.

You can also run the same engine from the command line without the MCP server:

```bash
python3 -m farofino_mcp.scan examples/SimpleToken.sol --format markdown
```

## Creating Your Own Test Contracts

You can create your own test contracts and place them in this directory or anywhere on your filesystem. The audit tools accept absolute or relative paths.

### Tips for Testing

1. **Start Simple**: Begin with pattern_analysis which requires no external tools
2. **Install Tools**: With Docker all tools are bundled; for local setups install Slither (`pip install slither-analyzer`) and Aderyn via Cyfrinup (`curl -LsSf https://raw.githubusercontent.com/Cyfrin/up/main/install | bash && CYFRINUP_ONLY_INSTALL=aderyn cyfrinup`)
3. **Compare Results**: Run multiple tools (pattern analysis, Slither, Aderyn) on the same contract to get different perspectives
4. **Test Edge Cases**: Create contracts with known vulnerabilities to verify detection
