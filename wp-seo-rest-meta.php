<?php
/**
 * Plugin Name: SEO Meta для REST API (Yoast / Rank Math)
 * Description: Регистрирует SEO мета-поля Yoast и Rank Math так, чтобы их можно
 *              было писать через WordPress REST API (например, скриптом wp_publish.py).
 * Version: 1.0
 *
 * УСТАНОВКА:
 *   Положите этот файл в папку  wp-content/mu-plugins/
 *   (если папки mu-plugins нет — создайте её). Активация не требуется:
 *   must-use плагины подключаются автоматически.
 *
 * БЕЗОПАСНОСТЬ:
 *   auth_callback разрешает запись только пользователям с правом edit_posts,
 *   то есть авторам/редакторам/администраторам. Обычные читатели писать не смогут.
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit; // защита от прямого вызова
}

add_action( 'init', function () {

    $auth = function () {
        return current_user_can( 'edit_posts' );
    };

    $register = function ( $key ) use ( $auth ) {
        foreach ( array( 'post', 'page' ) as $post_type ) {
            register_post_meta( $post_type, $key, array(
                'type'          => 'string',
                'single'        => true,
                'show_in_rest'  => true,
                'auth_callback' => $auth,
            ) );
        }
    };

    // Yoast SEO
    $register( '_yoast_wpseo_title' );
    $register( '_yoast_wpseo_metadesc' );
    $register( '_yoast_wpseo_focuskw' );

    // Rank Math
    $register( 'rank_math_title' );
    $register( 'rank_math_description' );
    $register( 'rank_math_focus_keyword' );

    // ACF "Автор публикации" (avtory) — relationship-поле на специалистов,
    // используется template-parts/content-single.php. avtory хранит массив ID
    // специалистов, _avtory — служебная ссылка ACF на ключ поля (без неё ACF не
    // распознает значение как relationship и блок автора не отрисуется).
    register_post_meta( 'post', 'avtory', array(
        'type'          => 'array',
        'single'        => true,
        'show_in_rest'  => array(
            'schema' => array(
                'type'  => 'array',
                'items' => array( 'type' => 'string' ),
            ),
        ),
        'auth_callback' => $auth,
    ) );
    register_post_meta( 'post', '_avtory', array(
        'type'          => 'string',
        'single'        => true,
        'show_in_rest'  => true,
        'auth_callback' => $auth,
    ) );
} );
