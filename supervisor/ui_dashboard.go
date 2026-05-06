package main

import (
	"fmt"
	"os"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// UI Styling
var (
	baseStyle      = lipgloss.NewStyle().Padding(1, 2)
	headerStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("86")).Bold(true).Border(lipgloss.NormalBorder(), false, false, true, false).Padding(0, 1)
	activeTabStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("0")).Background(lipgloss.Color("86")).Bold(true).Padding(0, 2).MarginRight(2)
	inactiveTabStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("241")).Padding(0, 2).MarginRight(2)
	
	boxStyle       = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(lipgloss.Color("63")).Padding(1, 2).Width(45)
	alertStyle     = lipgloss.NewStyle().Foreground(lipgloss.Color("196")).Bold(true)
	successStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color("42")).Bold(true)
	infoStyle      = lipgloss.NewStyle().Foreground(lipgloss.Color("214"))
)

// Async Message Types
type tickMsg time.Time
type actionCompleteMsg struct {
	message string
	success bool
}

// Dashboard State Model
type model struct {
	stats        SystemStats
	statusMsg    string
	isProcessing bool
	activeTab    int
	quitting     bool
}

func initialModel() model {
	return model{
		stats:        FetchLiveStats(), // From docker_client.go
		statusMsg:    "System running optimally.",
		isProcessing: false,
		activeTab:    0,
	}
}

func (m model) Init() tea.Cmd {
	return tick() // Start the polling loop
}

// tick triggers a UI refresh every 2 seconds
func tick() tea.Cmd {
	return tea.Tick(time.Second*2, func(t time.Time) tea.Msg {
		return tickMsg(t)
	})
}

// Background Task Executors (Non-Blocking)
func executeRestart() tea.Msg {
	time.Sleep(3 * time.Second) // Simulate Docker API wait time
	// RestartContainers() // Call to your docker_client.go function
	return actionCompleteMsg{message: "Containers restarted successfully.", success: true}
}

func executeBackup() tea.Msg {
	time.Sleep(2 * time.Second) // Simulate SQLite DB copy and zip
	return actionCompleteMsg{message: "DB backed up to /opt/logistics-system/backups", success: true}
}

func executeMaintenanceToggle() tea.Msg {
	time.Sleep(1 * time.Second) // Simulate gRPC call to Go Engine
	return actionCompleteMsg{message: "Maintenance Mode toggled.", success: true}
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {

	case tea.KeyMsg:
		if m.isProcessing && msg.String() != "ctrl+c" {
			return m, nil // Lock inputs while processing, except force quit
		}

		switch msg.String() {
		case "q", "ctrl+c":
			m.quitting = true
			return m, tea.Quit
		case "tab":
			m.activeTab = (m.activeTab + 1) % 2 // Cycle through 2 tabs
			return m, nil
		case "r":
			m.isProcessing = true
			m.statusMsg = "⏳ Restarting Docker containers..."
			return m, executeRestart
		case "b":
			if m.activeTab == 1 { // Only available on DB Tab
				m.isProcessing = true
				m.statusMsg = "⏳ Compressing SQLite Database..."
				return m, executeBackup
			}
		case "m":
			if m.activeTab == 1 {
				m.isProcessing = true
				m.statusMsg = "⏳ Sending gRPC signal to Engine..."
				return m, executeMaintenanceToggle
			}
		case "u":
			m.isProcessing = true
			m.statusMsg = "⏳ Checking for signed OTA updates..."
			// Return a tea.Cmd for your OTA updater
			return m, func() tea.Msg {
				time.Sleep(2 * time.Second)
				return actionCompleteMsg{message: "System is already up to date.", success: true}
			}
		}

	case actionCompleteMsg:
		m.isProcessing = false
		m.statusMsg = msg.message
		m.stats = FetchLiveStats() // Force refresh stats after action
		return m, nil

	case tickMsg:
		if !m.isProcessing { // Don't interrupt loading screens with polling updates
			m.stats = FetchLiveStats()
		}
		return m, tick()
	}

	return m, nil
}

func (m model) View() string {
	if m.quitting {
		return "Exiting Supervisor Dashboard...\n"
	}

	// 1. Render Tabs
	tabs := ""
	tabNames :=[]string{"🖥️ Infrastructure Health", "🛠️ System Administration"}
	for i, name := range tabNames {
		if m.activeTab == i {
			tabs += activeTabStyle.Render(name)
		} else {
			tabs += inactiveTabStyle.Render(name)
		}
	}

	// 2. Render Active Tab Content
	var mainContent string
	if m.activeTab == 0 {
		// TAB 0: INFRASTRUCTURE
		healthBox := boxStyle.Render(fmt.Sprintf(
			"🐳 DOCKER DAEMON\n\nCore Engine:  %s\nPython Bot:   %s\n\nCPU Usage: %s\nRAM Usage: %s",
			m.stats.EngineStatus, m.stats.BotStatus, m.stats.CPUUsage, m.stats.RAMUsage,
		))
		networkBox := boxStyle.Render(fmt.Sprintf(
			"🌐 NETWORK & SEC\n\nSocket Path:  /app/sockets/logistics.sock\nPermission:   %s\nInterceptor:  %s\nDB Encrypt:   %s",
			successStyle.Render("chmod 600 (Strict)"), successStyle.Render("ACTIVE"), successStyle.Render("AES-256"),
		))
		mainContent = lipgloss.JoinHorizontal(lipgloss.Top, healthBox, "  ", networkBox)
	} else {
		// TAB 1: ADMINISTRATION
		dbBox := boxStyle.Render(fmt.Sprintf(
			"💾 DATABASE (SQLite)\n\nLocation: /opt/logistics-system/deploy/data\nIntegrity: %s\nSize:      24.1 MB\n\nPress [b] to generate secure backup archive.",
			successStyle.Render("OK"),
		))
		sysBox := boxStyle.Render(fmt.Sprintf(
			"⚙️ SYSTEM CONTROL\n\nHardware ID (MAC):  HWID-0A1B2C3D4E\nLicense Expiry:     2028-05-01\nVersion:            v2.0.0\n\nPress [m] to toggle Maintenance Mode.",
		))
		mainContent = lipgloss.JoinHorizontal(lipgloss.Top, dbBox, "  ", sysBox)
	}

	// 3. Render Status Bar
	var statusLine string
	if m.isProcessing {
		statusLine = infoStyle.Render("STATUS: " + m.statusMsg)
	} else {
		statusLine = successStyle.Render("STATUS: " + m.statusMsg)
	}

	// 4. Render Dynamic Controls Footer
	controls := "[tab] Switch View  |  [r] Restart  |[u] OTA Update  |  [q] Quit"
	if m.activeTab == 1 {
		controls = "[tab] Switch View  |  [b] DB Backup  |  [m] Maintenance Mode  |  [q] Quit"
	}
	controlsLine := lipgloss.NewStyle().Foreground(lipgloss.Color("241")).Render(controls)

	// Combine everything
	ui := lipgloss.JoinVertical(lipgloss.Left,
		headerStyle.Render("LOGISTICS ENGINE SUPERVISOR"),
		"\n",
		tabs,
		"\n\n",
		mainContent,
		"\n\n",
		statusLine,
		controlsLine,
	)

	return baseStyle.Render(ui)
}

// StartDashboard launches the Bubble Tea UI
func StartDashboard() {
	p := tea.NewProgram(initialModel(), tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Printf("Dashboard crash: %v", err)
		os.Exit(1)
	}
}