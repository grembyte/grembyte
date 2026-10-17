// dummy_game.c - Minimal test target for memory scanning / write-access tracing
//
// Like most games, the player lives on the heap behind pointers:
//     g_game (global) -> Game -> player -> Character.gold
// so its address changes with every start and with every "New game".
#include <gtk/gtk.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

typedef struct {
    int32_t gold;
    int32_t hp;
    int32_t strength;
    int32_t dexterity;
    int32_t intelligence;
} Character;

typedef struct {
    char name[32];
    int32_t round;
    Character *player;
} Game;

static Game *g_game;

static GtkWidget *lbl_gold;
static GtkWidget *lbl_stats;

// noinline keeps the write instruction in its own, easy-to-find function
__attribute__((noinline)) static void add_gold(int32_t amount) {
    g_game->player->gold += amount;
}

__attribute__((noinline)) static void remove_gold(int32_t amount) {
    g_game->player->gold -= amount;
}

static int roll_die(int sides) {
    return (rand() % sides) + 1;
}

// 3d6 per attribute, classic pen-and-paper style
__attribute__((noinline)) static int roll_3d6(void) {
    return roll_die(6) + roll_die(6) + roll_die(6);
}

static void refresh_ui(void) {
    char buf[256];
    Character *player = g_game->player;

    snprintf(buf, sizeof(buf), "Gold: %d", player->gold);
    gtk_label_set_text(GTK_LABEL(lbl_gold), buf);

    snprintf(buf, sizeof(buf), "HP: %d\nSTR: %d\nDEX: %d\nINT: %d",
             player->hp, player->strength,
             player->dexterity, player->intelligence);
    gtk_label_set_text(GTK_LABEL(lbl_stats), buf);
}

// Creates a new player object somewhere else on the heap, as games do when
// loading a level or starting a new game.
static void new_player(void) {
    // A random number of other allocations moves the player around.
    int blocks = 1 + rand() % 200;
    for (int i = 0; i < blocks; i++) {
        void *block = malloc(16 + rand() % 512);
        if (rand() % 2)
            free(block);
    }

    Character *old = g_game->player;
    Character *player = calloc(1, sizeof(Character));
    player->gold = 100;
    player->hp = 50;
    player->strength = player->dexterity = player->intelligence = 10;
    g_game->player = player;
    g_game->round++;
    free(old);

    printf("round %d: gold @ %p\n", g_game->round, (void *)&player->gold);
    fflush(stdout);
}

static void on_gold_add(GtkWidget *w, gpointer data) {
    (void)w;
    add_gold(GPOINTER_TO_INT(data));
    refresh_ui();
}

static void on_gold_remove(GtkWidget *w, gpointer data) {
    (void)w;
    remove_gold(GPOINTER_TO_INT(data));
    refresh_ui();
}

static void on_roll_stats(GtkWidget *w, gpointer data) {
    (void)w; (void)data;
    Character *player = g_game->player;
    player->strength     = roll_3d6();
    player->dexterity    = roll_3d6();
    player->intelligence = roll_3d6();
    player->hp           = 20 + roll_die(20);
    refresh_ui();
}

static void on_new_game(GtkWidget *w, gpointer data) {
    (void)w; (void)data;
    new_player();
    refresh_ui();
}

static void on_activate(GtkApplication *app, gpointer user_data) {
    (void)user_data;

    GtkWidget *win  = gtk_application_window_new(app);
    gtk_window_set_title(GTK_WINDOW(win), "Dummy Game");
    gtk_window_set_default_size(GTK_WINDOW(win), 260, 280);

    GtkWidget *grid = gtk_grid_new();
    gtk_grid_set_row_spacing(GTK_GRID(grid), 8);
    gtk_grid_set_column_spacing(GTK_GRID(grid), 8);
    gtk_container_set_border_width(GTK_CONTAINER(grid), 12);
    gtk_container_add(GTK_CONTAINER(win), grid);

    lbl_gold  = gtk_label_new("");
    lbl_stats = gtk_label_new("");
    gtk_grid_attach(GTK_GRID(grid), lbl_gold,  0, 0, 2, 1);
    gtk_grid_attach(GTK_GRID(grid), lbl_stats, 0, 1, 2, 1);

    GtkWidget *btn_add = gtk_button_new_with_label("Gold +100");
    GtkWidget *btn_sub = gtk_button_new_with_label("Gold -100");
    GtkWidget *btn_roll = gtk_button_new_with_label("Roll stats (3d6)");
    GtkWidget *btn_new  = gtk_button_new_with_label("New game");

    g_signal_connect(btn_add,  "clicked", G_CALLBACK(on_gold_add),    GINT_TO_POINTER(100));
    g_signal_connect(btn_sub,  "clicked", G_CALLBACK(on_gold_remove), GINT_TO_POINTER(100));
    g_signal_connect(btn_roll, "clicked", G_CALLBACK(on_roll_stats),  NULL);
    g_signal_connect(btn_new,  "clicked", G_CALLBACK(on_new_game),    NULL);

    gtk_grid_attach(GTK_GRID(grid), btn_add,  0, 2, 1, 1);
    gtk_grid_attach(GTK_GRID(grid), btn_sub,  1, 2, 1, 1);
    gtk_grid_attach(GTK_GRID(grid), btn_roll, 0, 3, 2, 1);
    gtk_grid_attach(GTK_GRID(grid), btn_new,  0, 4, 2, 1);

    refresh_ui();
    gtk_widget_show_all(win);
}

int main(int argc, char **argv) {
    srand((unsigned)time(NULL));

    // Print PID and addresses so you can verify your scanner's results
    printf("PID: %d\n", getpid());
    printf("g_game    @ %p (pointer chain: g_game -> +%#zx player -> +%#zx gold)\n",
           (void *)&g_game, offsetof(Game, player), offsetof(Character, gold));
    g_game = calloc(1, sizeof(Game));
    snprintf(g_game->name, sizeof(g_game->name), "Dummy Game");
    new_player();

    // NON_UNIQUE: several instances may run, e.g. an old one while testing a restart.
    GtkApplication *app = gtk_application_new("local.dummygame",
                                              G_APPLICATION_NON_UNIQUE);
    g_signal_connect(app, "activate", G_CALLBACK(on_activate), NULL);
    int status = g_application_run(G_APPLICATION(app), argc, argv);
    g_object_unref(app);
    return status;
}