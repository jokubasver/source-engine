#ifndef GAMEPADUI_GLYPH_H
#define GAMEPADUI_GLYPH_H
#ifdef _WIN32
#pragma once
#endif

#include "gamepadui_interface.h"
#include "gamepadui_util.h"

#ifdef HL2_RETAIL // Steam input and Steam Controller are not supported in SDK2013 (Madi)
#include "steam/hl2/isteaminput.h"
#include "imageutils.h"
#endif // HL2_RETAIL

#include "inputsystem/iinputsystem.h"
#include "bitmap/bitmap.h"

class GamepadUIGlyph
{
public:
    GamepadUIGlyph()
    {
        m_nOriginTextures[0] = -1;
        m_nOriginTextures[1] = -1;
        m_szFallbackLabel[0] = '\0';
    }

    ~GamepadUIGlyph()
    {
        Cleanup();
    }

    bool SetupGlyph( int nSize, const char *pszAction, bool bBaseLight = false )
    {
        if ( !Q_strcmp( pszAction, m_szLastAction ) && nSize == m_nLastSize )
            return IsValid();

        Q_strncpy( m_szLastAction, pszAction, sizeof( m_szLastAction ) );
        m_nLastSize = nSize;

#ifdef HL2_RETAIL
        if ( SteamInputAvailable() )
        {
            m_bUsingStaticFallback = false;
            return SetupGlyphSteamInput( nSize, pszAction, bBaseLight );
        }
#endif

        m_bUsingStaticFallback = true;
        return SetupGlyphStatic( nSize, pszAction );
    }

    void SetFont( vgui::HFont hFont ) { m_hFont = hFont; }

    void PaintGlyph( int nX, int nY, int nSize, int nBaseAlpha )
    {
        if ( m_bUsingStaticFallback )
        {
            PaintFallbackGlyph( nX, nY, nSize, nBaseAlpha );
            return;
        }

        int nPressedAlpha = 255 - nBaseAlpha;

        if ( nBaseAlpha && m_nOriginTextures[0] > 0 )
        {
            vgui::surface()->DrawSetColor( Color( 255, 255, 255, nBaseAlpha ) );
            vgui::surface()->DrawSetTexture( m_nOriginTextures[0] );
            vgui::surface()->DrawTexturedRect( nX, nY, nX + nSize, nY + nSize );
        }

        if ( nPressedAlpha && m_nOriginTextures[1] > 0 )
        {
            vgui::surface()->DrawSetColor( Color( 255, 255, 255, nPressedAlpha ) );
            vgui::surface()->DrawSetTexture( m_nOriginTextures[1] );
            vgui::surface()->DrawTexturedRect( nX, nY, nX + nSize, nY + nSize );
        }

        vgui::surface()->DrawSetTexture(0);
    }

    bool IsValid()
    {
        if ( m_bUsingStaticFallback )
            return m_szFallbackLabel[0] != '\0';
        return m_nOriginTextures[0] > 0 && m_nOriginTextures[1] > 0;
    }

    void Cleanup()
    {
        for ( int i = 0; i < 2; i++ )
        {
            if ( m_nOriginTextures[ i ] > 0 )
                vgui::surface()->DestroyTextureID( m_nOriginTextures[ i ] );
            m_nOriginTextures[ i ] = -1;
        }

        m_szFallbackLabel[0] = '\0';

#ifdef HL2_RETAIL
        m_eActionOrigin = k_EInputActionOrigin_None;
#endif // HL2_RETAIL
    }

private:

#ifdef HL2_RETAIL
    bool SteamInputAvailable()
    {
        if ( !GamepadUI::GetInstance().GetSteamAPIContext() || !GamepadUI::GetInstance().GetSteamAPIContext()->SteamInput() )
            return false;

        uint64 nSteamInputHandles[STEAM_INPUT_MAX_COUNT];
        GamepadUI::GetInstance().GetSteamAPIContext()->SteamInput()->GetConnectedControllers( nSteamInputHandles );

        uint64 nController = g_pInputSystem->GetActiveSteamInputHandle();
        if ( !nController )
            nController = nSteamInputHandles[0];

        return nController != 0;
    }

    bool SetupGlyphSteamInput( int nSize, const char *pszAction, bool bBaseLight )
    {
        uint64 nSteamInputHandles[STEAM_INPUT_MAX_COUNT];
        GamepadUI::GetInstance().GetSteamAPIContext()->SteamInput()->GetConnectedControllers( nSteamInputHandles );

        uint64 nController = g_pInputSystem->GetActiveSteamInputHandle();
        if ( !nController )
            nController = nSteamInputHandles[0];

        if ( !nController )
            return false;

        InputActionSetHandle_t hActionSet = GamepadUI::GetInstance().GetSteamAPIContext()->SteamInput()->GetCurrentActionSet( nController );
        InputDigitalActionHandle_t hDigitalAction = GamepadUI::GetInstance().GetSteamAPIContext()->SteamInput()->GetDigitalActionHandle( pszAction );
        if ( !hDigitalAction )
        {
            Cleanup();
            return false;
        }

        EInputActionOrigin eOrigins[STEAM_INPUT_MAX_ORIGINS] = {};
        int nOriginCount = GamepadUI::GetInstance().GetSteamAPIContext()->SteamInput()->GetDigitalActionOrigins( nController, hActionSet, hDigitalAction, eOrigins );
        EInputActionOrigin eOrigin = eOrigins[0];
        if ( !nOriginCount || eOrigin == k_EInputActionOrigin_None )
        {
            Cleanup();
            return false;
        }

        if ( m_eActionOrigin == eOrigin )
            return IsValid();

        Cleanup();

        m_eActionOrigin = eOrigin;

        if ( !IsPowerOfTwo( nSize ) )
            nSize = NextPowerOfTwo( nSize );

        int nGlyphSize = 256;
        ESteamInputGlyphSize eGlyphSize = k_ESteamInputGlyphSize_Large;
        if (nSize <= 32)
        {
            eGlyphSize = k_ESteamInputGlyphSize_Small;
            nGlyphSize = 32;
        }
        else if (nSize <= 128)
        {
            eGlyphSize = k_ESteamInputGlyphSize_Medium;
            nGlyphSize = 128;
        }
        else
        {
            eGlyphSize = k_ESteamInputGlyphSize_Large;
            nGlyphSize = 256;
        }

        ESteamInputGlyphStyle kStyles[2] =
        {
            bBaseLight ? ESteamInputGlyphStyle_Light : ESteamInputGlyphStyle_Knockout,
            ESteamInputGlyphStyle_Dark,
        };

        for ( int i = 0; i < 2; i++ )
        {
            const char* pszGlyph = GamepadUI::GetInstance().GetSteamAPIContext()->SteamInput()->GetGlyphPNGForActionOrigin( eOrigin, eGlyphSize, kStyles[i] );
            if (!pszGlyph)
            {
                Cleanup();
                return false;
            }

            Bitmap_t bitmap;
            ConversionErrorType error = ImgUtl_LoadBitmap( pszGlyph, bitmap );
            if ( error != CE_SUCCESS )
            {
                Cleanup();
                return false;
            }

            ImgUtl_ResizeBitmap( bitmap, nSize, nSize, &bitmap );

            m_nOriginTextures[i] = vgui::surface()->CreateNewTextureID(true);
            if ( m_nOriginTextures[i] <= 0 )
            {
                Cleanup();
                return false;
            }
            g_pMatSystemSurface->DrawSetTextureRGBAEx2( m_nOriginTextures[i], bitmap.GetBits(), bitmap.Width(), bitmap.Height(), ImageFormat::IMAGE_FORMAT_RGBA8888, true, false );
        }

        return true;
    }
#endif // HL2_RETAIL

    const char *GetFallbackLabel( const char *pszAction )
    {
        if ( !Q_strcmp( pszAction, "menu_cancel" ) )  return "B";
        if ( !Q_strcmp( pszAction, "menu_select" ) )  return "A";
        if ( !Q_strcmp( pszAction, "menu_y" ) )       return "Y";
        if ( !Q_strcmp( pszAction, "menu_x" ) )       return "X";
        if ( !Q_strcmp( pszAction, "menu_lb" ) )      return "LB";
        if ( !Q_strcmp( pszAction, "menu_rb" ) )      return "RB";
        return NULL;
    }

    bool SetupGlyphStatic( int nSize, const char *pszAction )
    {
        const char *pszLabel = GetFallbackLabel( pszAction );
        if ( !pszLabel )
            return false;

        Cleanup();
        m_bUsingStaticFallback = true;
        Q_strncpy( m_szFallbackLabel, pszLabel, sizeof( m_szFallbackLabel ) );
        return true;
    }

    void PaintFallbackGlyph( int nX, int nY, int nSize, int nBaseAlpha )
    {
        int nTextAlpha = nBaseAlpha ? nBaseAlpha : 255;

        vgui::surface()->DrawSetColor( Color( 60, 60, 60, nTextAlpha ) );
        vgui::surface()->DrawFilledRectFade( nX + 1, nY + 1, nX + nSize - 1, nY + nSize - 1, 180, 180, true );

        vgui::surface()->DrawSetColor( Color( 200, 200, 200, nTextAlpha ) );
        vgui::surface()->DrawOutlinedRect( nX, nY, nX + nSize, nY + nSize );

        if ( m_hFont && m_szFallbackLabel[0] )
        {
            wchar_t wszLabel[8];
            V_UTF8ToUnicode( m_szFallbackLabel, wszLabel, sizeof( wszLabel ) );

            int tw, th;
            vgui::surface()->GetTextSize( m_hFont, wszLabel, tw, th );
            int tx = nX + ( nSize - tw ) / 2;
            int ty = nY + ( nSize - th ) / 2;

            vgui::surface()->DrawSetTextColor( Color( 255, 255, 255, nTextAlpha ) );
            vgui::surface()->DrawSetTextFont( m_hFont );
            vgui::surface()->DrawSetTextPos( tx, ty );
            vgui::surface()->DrawPrintText( wszLabel, V_wcslen( wszLabel ) );
        }
    }

#ifdef HL2_RETAIL
    EInputActionOrigin m_eActionOrigin = k_EInputActionOrigin_None;
#endif // HL2_RETAIL

    int m_nOriginTextures[2];
    bool m_bUsingStaticFallback = false;
    char m_szFallbackLabel[8] = {};
    char m_szLastAction[64] = {};
    int m_nLastSize = 0;
    vgui::HFont m_hFont = 0;
};

#endif
