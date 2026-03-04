# ApplyPilot Gemini API Setup

## ✅ Setup Status

The project has been successfully configured with the Gemini API key.

### Configuration Details

- **Environment File:** `~/.applypilot/.env`
- **LLM Provider:** Google Gemini API
- **API Key:** Configured and ready
- **Python Version:** 3.11.14+

### What Was Done

1. ✅ Created `.env` file at `~/.applypilot/.env`
2. ✅ Added Gemini API key to the environment configuration
3. ✅ Installed ApplyPilot package (v0.3.0) with all dependencies
4. ✅ Verified configuration loading

### Files Created

- `~/.applypilot/.env` - Environment configuration with Gemini API key

**Note:** The `.env` file is properly excluded from git via `.gitignore` (see `*.env` rule)

## Next Steps

### 1. Initialize ApplyPilot Profile
```bash
applypilot init
```
This will guide you through creating:
- Your profile (contact info, work authorization, skills)
- Search configuration (job titles, locations, boards)

### 2. Run Job Discovery & Scoring
```bash
applypilot run
```
This will:
- Discover jobs across 5+ boards
- Enrich job descriptions
- Score jobs against your profile
- Tailor resume per job
- Generate cover letters

### 3. Auto-Apply (if you have Claude Code CLI and Chrome)
```bash
applypilot apply
```
This will:
- Autonomously fill application forms
- Upload documents
- Answer screening questions
- Submit applications

## Gemini API Notes

- **Free Tier:** Google Gemini has a generous free tier for API usage
- **Authentication:** API key is loaded from `~/.applypilot/.env`
- **Model Used:** LiteLLM automatically routes to `gemini-pro` by default
- **Override Model:** Set `LLM_MODEL=gemini/gemini-2.0-flash` (or similar) in `.env` if needed

## Troubleshooting

### API Key Issues
If you need to update or change the API key:
```bash
# Edit the .env file
nano ~/.applypilot/.env
# Update GEMINI_API_KEY value
```

### Missing Dependencies
If you encounter import errors:
```bash
pip install applypilot
```

### Verify Setup
```bash
applypilot doctor
```
This command verifies all dependencies and configuration.

## Security Notes

- ✅ `.env` file is in `.gitignore` and will NOT be committed
- ✅ API keys are stored locally, not in the repository
- Keep your API key confidential
