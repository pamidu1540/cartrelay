package main

import (
	"fmt"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// ── Styles ────────────────────────────────────────────────────────────────────

var (
	titleStyle  = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("205"))
	labelStyle  = lipgloss.NewStyle().Width(14).Foreground(lipgloss.Color("244"))
	valueStyle  = lipgloss.NewStyle().Bold(true)
	footerStyle = lipgloss.NewStyle().Foreground(lipgloss.Color("240"))
	boxStyle    = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(lipgloss.Color("62")).
			Padding(0, 1)
)

// ── Model ─────────────────────────────────────────────────────────────────────

type tickMsg time.Time

type model struct {
	stats SystemStats
}

func initialModel() model {
	return model{stats: LatestStats()}
}

func (m model) Init() tea.Cmd {
	// FIX: The TUI ticks every 2 s and reads from statsAtom — it never blocks
	// on Docker. Stats are collected in a separate goroutine (StartStatsCollector).
	return tea.Tick(2*time.Second, func(t time.Time) tea.Msg { return tickMsg(t) })
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tickMsg:
		m.stats = LatestStats()
		return m, tea.Tick(2*time.Second, func(t time.Time) tea.Msg { return tickMsg(t) })

	case tea.KeyMsg:
		switch msg.String() {
		case "q", "ctrl+c":
			return m, tea.Quit
		case "r":
			go func() {
				if err := RestartContainers(); err != nil {
					// Will surface on next stats tick.
					_ = err
				}
			}()
		case "m":
			go func() {
				if _, err := GRPCToggleMaintenance(); err != nil {
					_ = err
				}
			}()
		case "u":
			go func() { _ = CheckAndApplyUpdate() }()
		}
	}
	return m, nil
}

func (m model) View() string {
	s := m.stats
	age := time.Since(s.CollectedAt).Truncate(time.Second)

	engineSection := boxStyle.Render(
		titleStyle.Render("⚙️  Engine") + "\n" +
			labelStyle.Render("Status") + valueStyle.Render(s.EngineStatus) + "\n" +
			labelStyle.Render("CPU") + valueStyle.Render(s.CPUUsage) + "\n" +
			labelStyle.Render("RAM") + valueStyle.Render(s.RAMUsage),
	)
	botSection := boxStyle.Render(
		titleStyle.Render("🤖  Bot") + "\n" +
			labelStyle.Render("Status") + valueStyle.Render(s.BotStatus),
	)

	footer := footerStyle.Render(
		fmt.Sprintf("\n[r] restart  [m] toggle maint  [u] update  [q] quit  — refreshed %s ago", age),
	)

	return lipgloss.JoinHorizontal(lipgloss.Top, engineSection, "  ", botSection) + footer
}

// StartDashboard launches the background stats collector and runs the TUI.
func StartDashboard() {
	// Collect stats every 3 s in the background.
	StartStatsCollector(3 * time.Second)

	p := tea.NewProgram(initialModel(), tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Printf("dashboard error: %v\n", err)
	}
}
